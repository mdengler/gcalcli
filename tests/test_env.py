from types import SimpleNamespace

import pytest

from gcalcli import env
from gcalcli import gcal


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Point every data/config/legacy location into tmp_path."""
    home = tmp_path / 'home'
    home.mkdir()
    data_dir = tmp_path / 'data' / 'gcalcli'
    config_dir = tmp_path / 'config' / 'gcalcli'
    config_dir.mkdir(parents=True)
    xdg_cache_home = tmp_path / 'xdg_cache'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    monkeypatch.setattr(env, 'default_data_dir', lambda: data_dir)
    return SimpleNamespace(
        home=home,
        data_dir=data_dir,
        config_dir=config_dir,
        xdg_cache_home=xdg_cache_home,
        xdg_cache=xdg_cache_home / 'gcalcli' / 'cache',
        legacy=home / '.gcalcli_cache',
    )


def resolve(name, config_dir):
    """Call the real (uncached) GoogleCalendarInterface.data_file_path."""
    stub_self = SimpleNamespace(
        options={'config_folder': config_dir},
        printer=SimpleNamespace(msg=lambda *args, **kwargs: None),
    )
    return gcal.GoogleCalendarInterface.data_file_path.__wrapped__(
        stub_self, name)


def test_cache_paths_without_xdg_cache_home(dirs):
    assert env.data_file_paths('cache', dirs.config_dir) == [
        (dirs.data_dir / 'cache', 0)]


def test_cache_paths_with_xdg_cache_home(dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    assert env.data_file_paths('cache', dirs.config_dir) == [
        (dirs.xdg_cache, 2), (dirs.data_dir / 'cache', 0)]


def test_cache_paths_list_every_existing_file_for_reset_cache(
        dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    (dirs.config_dir / 'cache').touch()
    dirs.legacy.touch()
    assert env.data_file_paths('cache', dirs.config_dir) == [
        (dirs.xdg_cache, 2),
        (dirs.config_dir / 'cache', 1),
        (dirs.data_dir / 'cache', 0),
        (dirs.legacy, -1),
    ]


@pytest.mark.parametrize('value', ['', 'relative/cache'])
def test_cache_paths_ignore_empty_or_relative_xdg_cache_home(
        dirs, monkeypatch, value):
    monkeypatch.setenv('XDG_CACHE_HOME', value)
    assert env.data_file_paths('cache', dirs.config_dir) == [
        (dirs.data_dir / 'cache', 0)]


def test_oauth_paths_ignore_xdg_cache_home(dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    assert env.data_file_paths('oauth', dirs.config_dir) == [
        (dirs.data_dir / 'oauth', 0)]


def test_resolve_cache_without_xdg_cache_home(dirs):
    assert resolve('cache', dirs.config_dir) == dirs.data_dir / 'cache'


def test_resolve_cache_prefers_config_dir_without_xdg_cache_home(dirs):
    (dirs.config_dir / 'cache').touch()
    assert resolve('cache', dirs.config_dir) == dirs.config_dir / 'cache'


def test_resolve_cache_prefers_xdg_cache_home_before_it_exists(
        dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    dirs.data_dir.mkdir(parents=True)
    (dirs.data_dir / 'cache').touch()
    assert resolve('cache', dirs.config_dir) == dirs.xdg_cache


def test_resolve_cache_prefers_xdg_cache_home_over_config_dir(
        dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    (dirs.config_dir / 'cache').touch()
    assert resolve('cache', dirs.config_dir) == dirs.xdg_cache


def test_resolve_cache_with_xdg_cache_home_leaves_legacy_file(
        dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    dirs.legacy.touch()
    assert resolve('cache', dirs.config_dir) == dirs.xdg_cache
    assert dirs.legacy.exists()


def test_resolve_oauth_ignores_xdg_cache_home(dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', str(dirs.xdg_cache_home))
    assert resolve('oauth', dirs.config_dir) == dirs.data_dir / 'oauth'


def test_resolve_cache_relative_xdg_cache_home_is_ignored(
        dirs, monkeypatch):
    monkeypatch.setenv('XDG_CACHE_HOME', 'relative/cache')
    assert resolve('cache', dirs.config_dir) == dirs.data_dir / 'cache'
