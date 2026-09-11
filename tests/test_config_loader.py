import time

import pytest
import yaml

from app.config_loader import ConfigValidationError, HotReloadableYaml, require


class TestRequire:
    def test_returns_value_when_valid(self):
        assert require({"x": 5}, "x", int, min=1, max=10) == 5

    def test_missing_required_key_raises_naming_the_key(self):
        with pytest.raises(ConfigValidationError, match="'missing_key'"):
            require({}, "missing_key", int)

    def test_missing_optional_key_returns_none(self):
        assert require({}, "opt", int, required=False) is None

    def test_wrong_type_raises_naming_value_and_expected_type(self):
        with pytest.raises(ConfigValidationError, match="expected int"):
            require({"x": "five"}, "x", int)

    def test_bool_rejected_for_int_type(self):
        # bool is a subclass of int in Python -- a stray `true` in YAML
        # must not silently pass an int check.
        with pytest.raises(ConfigValidationError):
            require({"x": True}, "x", int)

    def test_below_min_raises_naming_the_minimum(self):
        with pytest.raises(ConfigValidationError, match="minimum 1"):
            require({"x": 0}, "x", int, min=1)

    def test_above_max_raises_naming_the_maximum(self):
        with pytest.raises(ConfigValidationError, match="maximum 10"):
            require({"x": 11}, "x", int, max=10)

    def test_not_in_choices_raises(self):
        with pytest.raises(ConfigValidationError):
            require({"x": "z"}, "x", str, choices=["a", "b"])


class TestHotReloadableYaml:
    def test_loads_and_validates_on_first_access(self, tmp_path, monkeypatch):
        import app.config_loader as config_loader

        monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
        (tmp_path / "sample.yaml").write_text("value: 5\n", encoding="utf-8")

        seen = []
        store = HotReloadableYaml("sample.yaml", validate_fn=lambda d: seen.append(d))

        data = store.get()
        assert data == {"value": 5}
        assert seen == [{"value": 5}]

    def test_validation_error_propagates(self, tmp_path, monkeypatch):
        import app.config_loader as config_loader

        monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
        (tmp_path / "sample.yaml").write_text("value: -1\n", encoding="utf-8")

        def validate(d):
            require(d, "value", int, min=0)

        store = HotReloadableYaml("sample.yaml", validate_fn=validate)
        with pytest.raises(ConfigValidationError):
            store.get()

    def test_reloads_only_when_file_changes(self, tmp_path, monkeypatch):
        import app.config_loader as config_loader

        monkeypatch.setattr(config_loader, "CONFIG_DIR", tmp_path)
        path = tmp_path / "sample.yaml"
        path.write_text("value: 1\n", encoding="utf-8")

        load_count = []
        store = HotReloadableYaml("sample.yaml", validate_fn=lambda d: load_count.append(1))

        assert store.get()["value"] == 1
        assert store.get()["value"] == 1  # second read, same mtime -- no reparse
        assert len(load_count) == 1

        # Bump mtime forward explicitly -- some filesystems have coarse
        # mtime resolution and a same-second rewrite wouldn't otherwise
        # register as "changed".
        new_time = path.stat().st_mtime + 2
        path.write_text("value: 2\n", encoding="utf-8")
        import os
        os.utime(path, (new_time, new_time))

        assert store.get()["value"] == 2
        assert len(load_count) == 2
