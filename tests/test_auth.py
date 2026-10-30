import datetime
import io
import os
import pathlib
import shutil
import sys

try:
    import cPickle as pickle  # type: ignore
except Exception:
    import pickle

import google.auth.exceptions
import google.oauth2.credentials
import google.oauth2.reauth
import googleapiclient.discovery
import pytest

import gcalcli.cli
from gcalcli.argparsers import get_color_parser
from gcalcli import auth
from gcalcli.exceptions import GcalcliError
from gcalcli.gcal import GoogleCalendarInterface
from gcalcli.printer import Printer

TEST_DATA_DIR = pathlib.Path(__file__).parent / 'data'


def test_legacy_certs(tmpdir, gcali_patches, patched_google_reauth):
    tmpdir = pathlib.Path(tmpdir)
    oauth_filepath = tmpdir / 'oauth'
    shutil.copy(TEST_DATA_DIR / 'legacy_oauth_creds.json', oauth_filepath)
    gcal = gcali_patches.GCalI(data_path=tmpdir, refresh_cache=False)
    assert isinstance(
        gcal.get_cal_service(), googleapiclient.discovery.Resource
    )
    with open(oauth_filepath, 'rb') as gcalcli_oauth:
        try:
            pickle.load(gcalcli_oauth)
        except Exception as e:
            raise AssertionError(
                f"Couldn't load oauth file as updated pickle format: {e}"
            )


# --- Google rejecting the saved login (invalid_grant) ---

CAL_A = {'id': 'a@example.com', 'summary': 'Alpha', 'accessRole': 'owner'}
CAL_B = {'id': 'b@example.com', 'summary': 'Bravo', 'accessRole': 'reader'}
EXPIRED = datetime.datetime(2020, 1, 1)


def utcnow():
    # google-auth compares expiry against naive UTC times.
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class FakeRequest:
    """Stands in for an HttpRequest whose execute() hits `error`."""

    def __init__(self, error):
        self.error = error

    def execute(self, http=None):
        raise self.error


def invalid_grant_error():
    return google.auth.exceptions.RefreshError(
        'invalid_grant: Bad Request',
        {'error': 'invalid_grant', 'error_description': 'Bad Request'},
    )


def saved_credentials(name, expiry=None):
    return google.oauth2.credentials.Credentials(
        f'{name}-token',
        refresh_token=f'{name}-refresh',
        client_id='saved-id',
        client_secret='saved-secret',
        token_uri='https://oauth2.googleapis.com/token',
        expiry=expiry,
    )


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A config folder for gcalcli files, with HOME and XDG_DATA_HOME moved
    into tmp_path and XDG_CACHE_HOME unset, so no fallback path can reach the
    real home directory."""
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.delenv('XDG_CACHE_HOME', raising=False)
    path = tmp_path / 'config'
    path.mkdir()
    return path


def write_saved_login(config_dir, expiry=EXPIRED, age_days=9):
    oauth_filepath = config_dir / 'oauth'
    with oauth_filepath.open('wb') as gcalcli_oauth:
        pickle.dump(saved_credentials('old', expiry), gcalcli_oauth)
    signed_in = (
        datetime.datetime.now() - datetime.timedelta(days=age_days)
    ).timestamp()
    os.utime(oauth_filepath, (signed_in, signed_in))


def write_cache(config_dir, cals):
    with (config_dir / 'cache').open('wb') as cache:
        pickle.dump({'all_cals': cals}, cache)


def read_cache(config_dir):
    with (config_dir / 'cache').open('rb') as cache:
        return pickle.load(cache)['all_cals']


def read_saved_login(config_dir):
    with (config_dir / 'oauth').open('rb') as gcalcli_oauth:
        return pickle.load(gcalcli_oauth)


def make_gcal(config_dir, **options):
    """A real GoogleCalendarInterface reading files from `config_dir`."""
    options = {
        **vars(get_color_parser().parse_args([])),
        'config_folder': config_dir,
        'refresh_cache': False,
        'use_cache': True,
        'ignore_calendars': [],
        'auth_local_server': True,
        **options,
    }
    printer = Printer(conky=False, use_color=False, art_style='ascii')
    return GoogleCalendarInterface(printer=printer, **options)


@pytest.fixture
def refreshes(monkeypatch):
    """Record token refreshes instead of calling Google.

    Set `refreshes.error` to make Google reject them.
    """
    class Refreshes(list):
        error = None

    calls = Refreshes()

    def fake_refresh_grant(*args, **kwargs):
        calls.append(args)
        if calls.error:
            raise calls.error
        expiry = utcnow() + datetime.timedelta(hours=1)
        return ('fresh-token', 'old-refresh', expiry, {}, None)

    monkeypatch.setattr(
        google.oauth2.reauth, 'refresh_grant', fake_refresh_grant)
    return calls


@pytest.fixture
def sign_ins(monkeypatch):
    """Record calls to auth.authenticate instead of opening a browser."""
    calls = []

    def fake_authenticate(client_id, client_secret, printer, local):
        calls.append((client_id, client_secret))
        return saved_credentials('new')

    monkeypatch.setattr(auth, 'authenticate', fake_authenticate)
    return calls


@pytest.fixture
def dead_login(config_dir, refreshes):
    """A 9-day-old saved login that Google rejects, and a warm cache."""
    write_saved_login(config_dir)
    write_cache(config_dir, [CAL_A, CAL_B])
    refreshes.error = invalid_grant_error()
    return config_dir


@pytest.fixture
def at_terminal(monkeypatch):
    """Returns a function that makes stdin and stderr terminals, with `text`
    queued as input.  Call it in the test body: pytest's output capture
    replaces sys.stderr after fixtures run."""

    def answer(text):
        monkeypatch.setattr(sys, 'stdin', FakeTTY(text))
        monkeypatch.setattr(sys, 'stderr', FakeTTY())

    return answer


def new_login_sees(monkeypatch, cals):
    monkeypatch.setattr(
        GoogleCalendarInterface, '_fetch_calendar_list', lambda self: cals)


def test_is_invalid_grant():
    assert auth.is_invalid_grant(invalid_grant_error())
    assert not auth.is_invalid_grant(google.auth.exceptions.RefreshError(
        'invalid_client: Unauthorized', {'error': 'invalid_client'}))
    assert not auth.is_invalid_grant(
        google.auth.exceptions.RefreshError('no details'))


def test_invalid_grant_message_after_testing_expiry():
    assert auth.invalid_grant_message(
        signed_in=datetime.datetime(2026, 10, 9, 8, 0),
        now=datetime.datetime(2026, 10, 18, 13, 18),
    ) == (
        "Error: Google rejected gcalcli's saved login from 2026-10-09, "
        '9 days ago (invalid_grant).\n'
        'Google ends logins after 7 days while your Cloud app is in '
        '"Testing" mode.\n'
        '  Short-term fix:  gcalcli init\n'
        '  Long-term fix:   click "Publish app" at '
        'https://console.developers.google.com/auth/audience\n'
    )


def test_invalid_grant_message_recent_login():
    assert auth.invalid_grant_message(
        signed_in=datetime.datetime(2026, 10, 17, 8, 0),
        now=datetime.datetime(2026, 10, 18, 13, 18),
    ) == (
        "Error: Google rejected gcalcli's saved login from 2026-10-17, "
        '1 day ago (invalid_grant).\n'
        'Access may have been revoked: '
        'https://myaccount.google.com/connections\n'
        '  Fix:  gcalcli init\n'
    )


def test_cache_served_run_never_signs_in(dead_login, refreshes, at_terminal):
    at_terminal('y\n')

    gcal = make_gcal(dead_login)
    gcal.ListAllCalendars()

    assert gcal.all_cals == [CAL_A, CAL_B]
    assert refreshes == []
    assert sys.stdin.read() == 'y\n'


def test_changing_command_signs_in_before_reading_cache(
    dead_login, sign_ins, at_terminal, monkeypatch
):
    at_terminal('y\n')
    new_login_sees(monkeypatch, [CAL_A])

    gcal = make_gcal(dead_login, sign_in_first=True)

    assert sign_ins == [('saved-id', 'saved-secret')]
    assert gcal.all_cals == [CAL_A]
    assert gcal.cals == [CAL_A]
    assert read_cache(dead_login) == [CAL_A]


def test_read_command_signs_in_mid_run_and_carries_on(
    dead_login, sign_ins, at_terminal, monkeypatch
):
    gcal = make_gcal(dead_login)
    at_terminal('y\n')
    new_login_sees(monkeypatch, [CAL_B, CAL_A])

    assert gcal.get_cal_service() is not None

    assert sign_ins == [('saved-id', 'saved-secret')]
    assert read_saved_login(dead_login).refresh_token == 'new-refresh'
    assert read_cache(dead_login) == [CAL_B, CAL_A]


def test_sign_in_again_saves_cache_under_xdg_cache_home(
    dead_login, sign_ins, at_terminal, monkeypatch, tmp_path
):
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'xdg'))
    xdg_cache = tmp_path / 'xdg' / 'gcalcli'
    xdg_cache.mkdir(parents=True)
    write_cache(xdg_cache, [CAL_A, CAL_B])
    gcal = make_gcal(dead_login)
    at_terminal('y\n')
    new_login_sees(monkeypatch, [CAL_B, CAL_A])

    gcal.get_cal_service()

    assert read_cache(xdg_cache) == [CAL_B, CAL_A]


def test_read_command_mid_run_stops_when_calendar_missing(
    dead_login, sign_ins, at_terminal, monkeypatch
):
    gcal = make_gcal(dead_login)
    at_terminal('y\n')
    new_login_sees(monkeypatch, [CAL_A])

    with pytest.raises(GcalcliError) as exc_info:
        gcal.get_cal_service()

    assert 'Bravo' in str(exc_info.value)
    assert 'Run the command again' in str(exc_info.value)
    assert read_cache(dead_login) == [CAL_A]


def test_gate_headless_fails_without_prompting(
    dead_login, sign_ins, monkeypatch
):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('y\n'))
    gcal = make_gcal(dead_login)

    with pytest.raises(GcalcliError) as exc_info:
        gcal.get_cal_service()

    message = str(exc_info.value)
    assert '9 days ago (invalid_grant)' in message
    assert 'Short-term fix:  gcalcli init' in message
    assert 'Long-term fix:' in message
    assert sys.stdin.read() == 'y\n', 'headless run must not read stdin'
    assert sign_ins == []


def test_gate_no_prompt_when_stderr_redirected(
    dead_login, sign_ins, monkeypatch
):
    # A person at the terminal couldn't see a prompt written to a log file.
    monkeypatch.setattr(sys, 'stdin', FakeTTY('y\n'))
    monkeypatch.setattr(sys, 'stderr', io.StringIO())
    gcal = make_gcal(dead_login)

    with pytest.raises(GcalcliError):
        gcal.get_cal_service()

    assert sys.stdin.read() == 'y\n'
    assert sign_ins == []


def test_missing_login_fails_when_stderr_redirected(
    config_dir, refreshes, sign_ins, monkeypatch
):
    # Nobody would see the sign-in link, so the browser flow would wait
    # forever (e.g. under cron).
    monkeypatch.setattr(sys, 'stdin', FakeTTY())
    monkeypatch.setattr(sys, 'stderr', io.StringIO())
    write_cache(config_dir, [CAL_A])
    gcal = make_gcal(config_dir, client_id='id', client_secret='secret')

    with pytest.raises(GcalcliError, match='Fix:  gcalcli init'):
        gcal.get_cal_service()

    assert sign_ins == []


def test_missing_login_signs_in_with_stdin_redirected(
    config_dir, refreshes, sign_ins, monkeypatch
):
    # The link still shows, e.g. for `gcalcli calw </dev/null`.
    monkeypatch.setattr(sys, 'stdin', io.StringIO())
    monkeypatch.setattr(sys, 'stderr', FakeTTY())
    write_cache(config_dir, [CAL_A])
    gcal = make_gcal(config_dir, client_id='id', client_secret='secret')

    gcal.get_cal_service()

    assert sign_ins == [('id', 'secret')]


@pytest.mark.parametrize('answer', ['n\n', ''])
def test_gate_declined(dead_login, sign_ins, at_terminal, answer):
    gcal = make_gcal(dead_login)
    at_terminal(answer)

    with pytest.raises(GcalcliError, match='gcalcli init'):
        gcal.get_cal_service()

    assert sign_ins == []
    assert read_saved_login(dead_login).refresh_token == 'old-refresh'


def test_gate_passes_other_refresh_errors(dead_login, refreshes):
    refreshes.error = google.auth.exceptions.RefreshError(
        'invalid_client: Unauthorized', {'error': 'invalid_client'})
    gcal = make_gcal(dead_login)

    with pytest.raises(google.auth.exceptions.RefreshError) as exc_info:
        gcal.get_cal_service()

    assert exc_info.value is refreshes.error


def test_invalid_grant_mid_request_fails_without_prompting(
    config_dir, refreshes, sign_ins, at_terminal
):
    write_saved_login(config_dir, expiry=None)
    write_cache(config_dir, [CAL_A])
    gcal = make_gcal(config_dir)
    gcal.get_cal_service()
    at_terminal('y\n')

    with pytest.raises(GcalcliError, match='invalid_grant'):
        gcal._retry_with_backoff(FakeRequest(invalid_grant_error()))

    assert sys.stdin.read() == 'y\n'
    assert sign_ins == []


def test_retry_passes_other_refresh_errors(config_dir, refreshes):
    write_saved_login(config_dir, expiry=None)
    write_cache(config_dir, [CAL_A])
    gcal = make_gcal(config_dir)
    error = google.auth.exceptions.RefreshError(
        'invalid_client: Unauthorized', {'error': 'invalid_client'})

    with pytest.raises(google.auth.exceptions.RefreshError) as exc_info:
        gcal._retry_with_backoff(FakeRequest(error))

    assert exc_info.value is error


def test_gate_refreshes_login_with_unknown_expiry(config_dir, refreshes):
    # Legacy logins have no expiry, so only a refresh can tell if they work.
    write_saved_login(config_dir, expiry=None)
    write_cache(config_dir, [CAL_A])

    make_gcal(config_dir).get_cal_service()

    assert len(refreshes) == 1


def test_gate_skips_refresh_for_valid_login(config_dir, refreshes):
    write_saved_login(
        config_dir, expiry=utcnow() + datetime.timedelta(minutes=30))
    write_cache(config_dir, [CAL_A])

    make_gcal(config_dir).get_cal_service()

    assert refreshes == []


def test_legacy_login_conversion_keeps_timestamp(config_dir):
    oauth_filepath = config_dir / 'oauth'
    shutil.copy(TEST_DATA_DIR / 'legacy_oauth_creds.json', oauth_filepath)
    signed_in = (
        datetime.datetime.now() - datetime.timedelta(days=9)
    ).timestamp()
    os.utime(oauth_filepath, (signed_in, signed_in))
    gcal = make_gcal(config_dir, do_eager_init=False)

    gcal._load_credentials()

    assert read_saved_login(config_dir).refresh_token
    assert oauth_filepath.stat().st_mtime == signed_in


def test_init_reuses_saved_client(
    config_dir, refreshes, sign_ins, monkeypatch
):
    write_saved_login(config_dir)
    gcal = make_gcal(config_dir, do_eager_init=False)
    # Only one answer: if init asked for a client ID too, input() would fail.
    monkeypatch.setattr(sys, 'stdin', io.StringIO('y\n'))

    gcal.SetupAuth()

    assert sign_ins == [('saved-id', 'saved-secret')]


@pytest.mark.parametrize('argv, sign_in_first', [
    (['list'], False),
    (['calw'], False),
    (['agenda'], False),
    (['search', 'x'], False),
    (['add'], True),
    (['quick', 'x'], True),
    (['import'], True),
    (['edit', 'x'], True),
    (['delete', 'x'], True),
    (['agendaupdate'], True),
])
def test_cli_setup(config_dir, monkeypatch, capsys, argv, sign_in_first):
    """cli signs in first only for commands that change the calendar, and
    reports errors raised during setup without a traceback."""
    seen = {}

    def failing_interface(**kwargs):
        seen.update(kwargs)
        raise GcalcliError('Error: setup failed\n')

    monkeypatch.setenv('GCALCLI_CONFIG', str(config_dir))
    monkeypatch.setattr(gcalcli.cli, 'GoogleCalendarInterface',
                        failing_interface)
    monkeypatch.setattr(sys, 'argv', ['gcalcli'] + argv)

    with pytest.raises(SystemExit) as exc_info:
        gcalcli.cli.main()

    assert exc_info.value.code == 1
    assert 'Error: setup failed' in capsys.readouterr().err
    assert seen['sign_in_first'] is sign_in_first
