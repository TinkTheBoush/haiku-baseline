import jax
import jax.numpy as jnp
import haiku as hk
import numpy as np
import optax
from einops import rearrange, reduce, repeat

from haiku_baselines.DQN.base_class import Q_Network_Family
from haiku_baselines.QRDQN.network import Model
from haiku_baselines.common.Module import PreProcess

from haiku_baselines.common.utils import hard_update, convert_jax, print_param, q_log_pi
from haiku_baselines.common.losses import QuantileHuberLosses

class QRDQN(Q_Network_Family):
    def __init__(self, env, gamma=0.995, learning_rate=3e-4, buffer_size=100000, exploration_fraction=0.3, n_support = 200, delta = 0.1,
                 exploration_final_eps=0.02, exploration_initial_eps=1.0, train_freq=1, gradient_steps=1, batch_size=32, double_q=True,
                 dueling_model = False, n_step = 1, learning_starts=1000, target_network_update_freq=2000, prioritized_replay=False,
                 prioritized_replay_alpha=0.6, prioritized_replay_beta0=0.4, prioritized_replay_eps=1e-6, 
                 param_noise=False, munchausen=False, log_interval=200, tensorboard_log=None, _init_setup_model=True, policy_kwargs=None, 
                 full_tensorboard_log=False, seed=None, optimizer = 'adamw', compress_memory = False):
        
        super(QRDQN, self).__init__(env, gamma, learning_rate, buffer_size, exploration_fraction,
                 exploration_final_eps, exploration_initial_eps, train_freq, gradient_steps, batch_size, double_q,
                 dueling_model, n_step, learning_starts, target_network_update_freq, prioritized_replay,
                 prioritized_replay_alpha, prioritized_replay_beta0, prioritized_replay_eps, 
                 param_noise, munchausen, log_interval, tensorboard_log, _init_setup_model, policy_kwargs, 
                 full_tensorboard_log, seed, optimizer, compress_memory)
        
        self.n_support = n_support
        self.delta = delta
        
        if _init_setup_model:
            self.setup_model() 
            
    def setup_model(self):
        self.policy_kwargs = {} if self.policy_kwargs is None else self.policy_kwargs
        if 'cnn_mode' in self.policy_kwargs.keys():
            cnn_mode = self.policy_kwargs['cnn_mode']
            del self.policy_kwargs['cnn_mode']
        self.preproc = hk.transform(lambda x: PreProcess(self.observation_space, cnn_mode=cnn_mode)(x))
        self.model = hk.transform(lambda x: Model(self.action_size,
                           dueling=self.dueling_model,noisy=self.param_noise,support_n=self.n_support,
                           **self.policy_kwargs)(x))
        pre_param = self.preproc.init(next(self.key_seq),
                            [np.zeros((1,*o),dtype=np.float32) for o in self.observation_space])
        model_param = self.model.init(next(self.key_seq),
                            self.preproc.apply(pre_param, 
                            None, [np.zeros((1,*o),dtype=np.float32) for o in self.observation_space]))
        self.params = hk.data_structures.merge(pre_param, model_param)
        self.target_params = self.params
        
        self.opt_state = self.optimizer.init(self.params)
        
        if not self.dueling_model:
            self.quantile = (jnp.linspace(0.0,1.0,self.n_support+1)[1:] + jnp.linspace(0.0,1.0,self.n_support+1)[:-1])/2.0   # [support]
            self.quantile = jax.device_put(jnp.expand_dims(self.quantile,axis=(0,1)))                                        # [1 x 1 x support]
        else:
            self.quantile = (jnp.linspace(0.0,1.0,self.n_support*self.n_support+1)[1:] + jnp.linspace(0.0,1.0,self.n_support*self.n_support+1)[:-1])/2.0   # [support]
            self.quantile = jax.device_put(jnp.expand_dims(self.quantile,axis=(0,1)))                                        # [1 x 1 x support]
        
        print("----------------------model----------------------")
        print_param('preprocess',pre_param)
        print_param('model',model_param)
        print("loss : quaile_huber_loss")
        print("-------------------------------------------------")

        self.get_q = jax.jit(self.get_q)
        self._get_actions = jax.jit(self._get_actions)
        self._loss = jax.jit(self._loss)
        self._target = jax.jit(self._target)
        self._train_step = jax.jit(self._train_step)
    
    def get_q(self, params, obses, key = None) -> jnp.ndarray:
        return self.model.apply(params, key, self.preproc.apply(params, key, obses))
        
    def _get_actions(self, params, obses, key = None) -> jnp.ndarray:
        return jnp.expand_dims(jnp.argmax(
               jnp.mean(self.get_q(params,convert_jax(obses),key),axis=2)
               ,axis=1),axis=1)
    
    def train_step(self, steps, gradient_steps):
        for _ in range(gradient_steps):
            if self.prioritized_replay:
                data = self.replay_buffer.sample(self.batch_size,self.prioritized_replay_beta0)
            else:
                data = self.replay_buffer.sample(self.batch_size)
            
            self.params, self.target_params, self.opt_state, loss, t_mean, new_priorities = \
                self._train_step(self.params, self.target_params, self.opt_state, steps, 
                                 next(self.key_seq) if self.param_noise or self.munchausen else None,**data)
            
            if self.prioritized_replay:
                self.replay_buffer.update_priorities(data['indexes'], new_priorities)
            
        if self.summary and steps % self.log_interval == 0:
            self.summary.add_scalar("loss/qloss", loss, steps)
            self.summary.add_scalar("loss/targets", t_mean, steps)
            
        return loss

    def _train_step(self, params, target_params, opt_state, steps, key, 
                    obses, actions, rewards, nxtobses, dones, weights=1, indexes=None):
        obses = convert_jax(obses); nxtobses = convert_jax(nxtobses); actions = jnp.expand_dims(actions.astype(jnp.int32),axis=2); not_dones = 1.0 - dones
        targets = self._target(params, target_params, obses, actions, rewards, nxtobses, not_dones, key)
        (loss,abs_error), grad = jax.value_and_grad(self._loss,has_aux = True)(params, obses, actions, targets, weights, key)
        updates, opt_state = self.optimizer.update(grad, opt_state, params=params)
        params = optax.apply_updates(params, updates)
        target_params = hard_update(params, target_params, steps, self.target_network_update_freq)
        new_priorities = None
        if self.prioritized_replay:
            new_priorities = abs_error + self.prioritized_replay_eps
        return params, target_params, opt_state, loss, jnp.mean(targets), new_priorities
    
    def _loss(self, params, obses, actions, targets, weights, key):
        theta_loss_tile = jnp.take_along_axis(self.get_q(params, obses, key), actions, axis=1)  # batch x 1 x support
        logit_valid_tile = jnp.expand_dims(targets,axis=2)                                      # batch x support x 1
        loss = QuantileHuberLosses(theta_loss_tile, logit_valid_tile, self.quantile, self.delta)
        return jnp.mean(loss * weights / jnp.max(weights)), loss #remove weight multiply cpprb weight is something wrong
    
    def _target(self,params, target_params, obses, actions, rewards, nxtobses, not_dones, key):
        next_q = self.get_q(target_params,nxtobses,key)
            
        if self.munchausen:
            if self.double_q:
                next_q_mean = jnp.mean(self.get_q(params,nxtobses,key),axis=2)
            else:
                next_q_mean = jnp.mean(next_q,axis=2)
            next_sub_q, tau_log_pi_next = q_log_pi(next_q_mean, self.munchausen_entropy_tau)
            pi_next = jax.nn.softmax(next_sub_q/self.munchausen_entropy_tau,axis=1)
            p_cuml = jnp.expand_dims(jnp.cumsum(pi_next,axis=1),axis=2).tile(32)
            r = jax.random.uniform(key, (32,1,32), dtype=p_cuml.dtype)
            ind = jnp.swapaxes(jax.vmap(jax.vmap(lambda p,r: jnp.searchsorted(p, r),in_axes=(1,1)))(p_cuml,r),1,2)
            sampled_q = jnp.take_along_axis(next_q - jnp.expand_dims(tau_log_pi_next,axis=2),ind,axis=1).squeeze()
            next_vals = sampled_q * not_dones
            
            if self.double_q:
                q_k_targets = jnp.mean(self.get_q(params,obses,key),axis=2)
            else:
                q_k_targets = jnp.mean(self.get_q(target_params,obses,key),axis=2)
            q_k_targets = jnp.mean(self.get_q(target_params,obses,key),axis=2)
            q_sub_targets, tau_log_pi = q_log_pi(q_k_targets, self.munchausen_entropy_tau)
            log_pi = q_sub_targets - self.munchausen_entropy_tau*tau_log_pi
            munchausen_addon = jnp.take_along_axis(log_pi,jnp.squeeze(actions,axis=2),axis=1)
            
            rewards = rewards + self.munchausen_alpha*jnp.clip(munchausen_addon, a_min=-1, a_max=0)
        else:
            if self.double_q:
                next_actions = jnp.expand_dims(jnp.argmax(jnp.mean(self.get_q(params,nxtobses,key),axis=2),axis=1),axis=(1,2))
            else:
                next_actions = jnp.expand_dims(jnp.argmax(jnp.mean(next_q,axis=2),axis=1),axis=(1,2))
            next_vals = not_dones * jnp.squeeze(jnp.take_along_axis(next_q, next_actions, axis=1)) # batch x support
        return (next_vals * self._gamma) + rewards                                                 # batch x support

    
    def learn(self, total_timesteps, callback=None, log_interval=100, tb_log_name="QRDQN",
              reset_num_timesteps=True, replay_wrapper=None):
        tb_log_name = tb_log_name + "({:d})".format(self.n_support)
        super().learn(total_timesteps, callback, log_interval, tb_log_name, reset_num_timesteps, replay_wrapper)