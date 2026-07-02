def median(nums):
    # BUG: does not sort the input, and does not average the two middle
    # values for even-length lists.
    n = len(nums)
    return nums[n // 2]
