from ateam.globs import expand, is_valid_pattern, matches


def test_matches_double_star():
    assert matches("src/api/**", "src/api/books/routes.py")
    assert matches("src/api/**", "src/api/x.py")
    assert not matches("src/api/**", "src/data/books.py")
    assert matches("src/data/books.py", "src/data/books.py")


def test_invalid_patterns():
    assert all(not is_valid_pattern(p) for p in ["/abs/x", "./src/**", "src/", "src/../x", ""])
    assert is_valid_pattern("src/**/*.py")


def test_expand_lists_relative_files(target_repo):
    assert expand(["src/api/**"], target_repo) == ["src/api/x.py"]
    assert expand(["nope/**"], target_repo) == []
