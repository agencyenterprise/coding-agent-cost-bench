from mathutils import median


def test_odd_sorted():
    assert median([3, 1, 2]) == 2


def test_even_average():
    assert median([4, 1, 3, 2]) == 2.5


def test_unsorted():
    assert median([5, 2, 9, 1, 7]) == 5
