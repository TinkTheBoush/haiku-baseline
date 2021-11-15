import jax
import numpy as np
import jax.numpy as jnp

@jax.jit
def unique(sorted_array: jnp.ndarray) -> jnp.ndarray:
    """
    More efficient implementation of np.unique for sorted arrays
    :param sorted_array: (np.ndarray)
    :return:(np.ndarray) sorted_array without duplicate elements
    """
    if len(sorted_array) == 1:
        return sorted_array
    left = sorted_array[:-1]
    right = sorted_array[1:]
    uniques = np.append(right != left, True)
    return sorted_array[uniques]


class SegmentTree(object):
    def __init__(self, capacity, operation, neutral_element):
        """
        Build a Segment Tree data structure.

        https://en.wikipedia.org/wiki/Segment_tree

        Can be used as regular array that supports Index arrays, but with two
        important differences:

            a) setting item's value is slightly slower.
               It is O(lg capacity) instead of O(1).
            b) user has access to an efficient ( O(log segment size) )
               `reduce` operation which reduces `operation` over
               a contiguous subsequence of items in the array.

        :param capacity: (int) Total size of the array - must be a power of two.
        :param operation: (lambda (Any, Any): Any) operation for combining elements (eg. sum, max) must form a
            mathematical group together with the set of possible values for array elements (i.e. be associative)
        :param neutral_element: (Any) neutral element for the operation above. eg. float('-inf') for max and 0 for sum.
        """
        assert capacity > 0 and capacity & (capacity - 1) == 0, "capacity must be positive and a power of 2."
        self._capacity = capacity
        self._value = jnp.full(2*capacity,neutral_element) #[neutral_element for _ in range(2 * capacity)]
        self._operation = operation
        self.neutral_element = neutral_element
        
        self._reduce_helper = jax.jit(self._reduce_helper)
        self._setitem_helper = jax.jit(self._setitem_helper)

    def _reduce_helper(self, _value, start, end, node, node_start, node_end):
        if start == node_start and end == node_end:
            return _value[node]
        
        mid = (node_start + node_end) // 2
        if end <= mid:
            return self._reduce_helper(_value, start, end, 2 * node, node_start, mid)
        else:
            if mid + 1 <= start:
                return self._reduce_helper(_value, start, end, 2 * node + 1, mid + 1, node_end)
            else:
                return self._operation(
                    self._reduce_helper(_value, start, mid, 2 * node, node_start, mid),
                    self._reduce_helper(_value, mid + 1, end, 2 * node + 1, mid + 1, node_end)
                )

    def reduce(self, start=0, end=None):
        """
        Returns result of applying `self.operation`
        to a contiguous subsequence of the array.

            self.operation(arr[start], operation(arr[start+1], operation(... arr[end])))

        :param start: (int) beginning of the subsequence
        :param end: (int) end of the subsequences
        :return: (Any) result of reducing self.operation over the specified range of array elements.
        """
        if end is None:
            end = self._capacity
        if end < 0:
            end += self._capacity
        end -= 1
        return self._reduce_helper(self._value, start, end, 1, 0, self._capacity - 1)

    def _setitem_helper(self, _value, idxs):
        idxs = unique(idxs // 2)
        while len(idxs) > 1 or idxs[0] > 0:
            # as long as there are non-zero indexes, update the corresponding values
            _value.at[idxs].set(self._operation(
                _value[2 * idxs],
                _value[2 * idxs + 1]
            ))
            # go up one level in the tree and remove duplicate indexes
            idxs = unique(idxs // 2)
        return _value
        
    def __setitem__(self, idx, val):
        # indexes of the leaf
        idxs = idx + self._capacity
        
        self._value.at[idxs].set(val)
        if isinstance(idxs, int):
            idxs = jnp.array([idxs])
        # go up one level in the tree and remove duplicate indexes
        self._value = self._setitem_helper(self._value, idxs)

    def __getitem__(self, idx):
        assert jnp.max(idx) < self._capacity
        assert 0 <= jnp.min(idx)
        return self._value[self._capacity + idx]


class SumSegmentTree(SegmentTree):
    def __init__(self, capacity):
        super(SumSegmentTree, self).__init__(
            capacity=capacity,
            operation=jnp.add,
            neutral_element=0.0
        )
        
        self._find_prefixsum_idx_helper = jax.jit(self._find_prefixsum_idx_helper)

    def sum(self, start=0, end=None):
        """
        Returns arr[start] + ... + arr[end]

        :param start: (int) start position of the reduction (must be >= 0)
        :param end: (int) end position of the reduction (must be < len(arr), can be None for len(arr) - 1)
        :return: (Any) reduction of SumSegmentTree
        """
        return super(SumSegmentTree, self).reduce(start, end)

    def _find_prefixsum_idx_helper(self, _value ,prefixsum):
        idx = jnp.ones(len(prefixsum), dtype=int)
        cont = jnp.ones(len(prefixsum), dtype=bool)

        while jnp.any(cont):  # while not all nodes are leafs
            idx.at[cont].set(2 * idx[cont])
            prefixsum_new = jnp.where(_value[idx] <= prefixsum, prefixsum - _value[idx], prefixsum)
            # prepare update of prefixsum for all right children
            idx = jnp.where(jnp.logical_or(_value[idx] > prefixsum, jnp.logical_not(cont)), idx, idx + 1)
            # Select child node for non-leaf nodes
            prefixsum = prefixsum_new
            # update prefixsum
            cont = idx < self._capacity
            # collect leafs
        return idx - self._capacity
    
    def find_prefixsum_idx(self, prefixsum):
        """
        Find the highest index `i` in the array such that
            sum(arr[0] + arr[1] + ... + arr[i - i]) <= prefixsum for each entry in prefixsum

        if array values are probabilities, this function
        allows to sample indexes according to the discrete
        probability efficiently.

        :param prefixsum: (np.ndarray) float upper bounds on the sum of array prefix
        :return: (np.ndarray) highest indexes satisfying the prefixsum constraint
        """
        if isinstance(prefixsum, float):
            prefixsum = jnp.array([prefixsum])
        assert 0 <= jnp.min(prefixsum)
        assert jnp.max(prefixsum) <= self.sum() + 1e-5
        assert isinstance(prefixsum[0], float)
        return self._find_prefixsum_idx_helper(self._value,prefixsum)



class MinSegmentTree(SegmentTree):
    def __init__(self, capacity):
        super(MinSegmentTree, self).__init__(
            capacity=capacity,
            operation=jnp.minimum,
            neutral_element=float('inf')
        )

    def min(self, start=0, end=None):
        """
        Returns min(arr[start], ...,  arr[end])

        :param start: (int) start position of the reduction (must be >= 0)
        :param end: (int) end position of the reduction (must be < len(arr), can be None for len(arr) - 1)
        :return: (Any) reduction of MinSegmentTree
        """
        return super(MinSegmentTree, self).reduce(start, end)