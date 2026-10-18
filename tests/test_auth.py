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
import google_auth_httplib2
import googleapiclient.discovery
import pytest

import gcalcli.cli
from gcalcli import auth
from gcalcli.exceptions import GcalcliError

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


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


class FakeRequest:
    """Stands in for an HttpRequest whose first execute() hits `error`."""

    def __init__(self, error):
        self.error = error
        self.https = []

    def execute(self, http=None):
        self.https.append(http)
        if len(self.https) == 1:
            raise self.error
        return {'items': []}


def invalid_grant_error():
    return google.auth.exceptions.RefreshError(
        'invalid_grant: Bad Request',
        {'error': 'invalid_grant', 'error_description': 'Bad Request'},
    )


def saved_credentials(name):
    return google.oauth2.credentials.Credentials(
        f'{name}-token',
        refresh_token=f'{name}-refresh',
        client_id='saved-id',
        client_secret='saved-secret',
        token_uri='https://oauth2.googleapis.com/token',
    )


@pytest.fixture
def gcal_with_dead_login(tmp_path, PatchedGCalI):
    """A GoogleCalendarInterface whose saved login is 9 days old."""
    oauth_filepath = tmp_path / 'oauth'
    with oauth_filepath.open('wb') as gcalcli_oauth:
        pickle.dump(saved_credentials('old'), gcalcli_oauth)
    nine_days_ago = (
        datetime.datetime.now() - datetime.timedelta(days=9)
    ).timestamp()
    os.utime(oauth_filepath, (nine_days_ago, nine_days_ago))
    gcal = PatchedGCalI(data_path=tmp_path, auth_local_server=True)
    gcal._load_credentials()
    return gcal


@pytest.fixture
def sign_ins(monkeypatch):
    """Record calls to auth.authenticate instead of opening a browser."""
    calls = []

    def fake_authenticate(client_id, client_secret, printer, local):
        calls.append((client_id, client_secret))
        return saved_credentials('new')

    monkeypatch.setattr(auth, 'authenticate', fake_authenticate)
    return calls


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


def test_invalid_grant_headless_fails_without_prompting(
    gcal_with_dead_login, sign_ins, monkeypatch
):
    monkeypatch.setattr(sys, 'stdin', io.StringIO('y\n'))
    request = FakeRequest(invalid_grant_error())

    with pytest.raises(GcalcliError) as exc_info:
        gcal_with_dead_login._retry_with_backoff(request)

    message = str(exc_info.value)
    assert '9 days ago (invalid_grant)' in message
    assert 'Short-term fix:  gcalcli init' in message
    assert 'Long-term fix:' in message
    assert sys.stdin.read() == 'y\n', 'headless run must not read stdin'
    assert sign_ins == []
    assert len(request.https) == 1


def test_invalid_grant_no_prompt_when_stderr_redirected(
    gcal_with_dead_login, sign_ins, monkeypatch
):
    # A person at the terminal couldn't see a prompt written to a log file.
    monkeypatch.setattr(sys, 'stdin', FakeTTY('y\n'))
    monkeypatch.setattr(sys, 'stderr', io.StringIO())

    with pytest.raises(GcalcliError):
        gcal_with_dead_login._retry_with_backoff(
            FakeRequest(invalid_grant_error()))

    assert sys.stdin.read() == 'y\n'
    assert sign_ins == []


def test_invalid_grant_interactive_signs_in_again_and_retries(
    gcal_with_dead_login, sign_ins, monkeypatch, tmp_path
):
    monkeypatch.setattr(sys, 'stdin', FakeTTY('\n'))
    monkeypatch.setattr(sys, 'stderr', FakeTTY())
    request = FakeRequest(invalid_grant_error())

    assert gcal_with_dead_login._retry_with_backoff(request) == {'items': []}

    assert sign_ins == [('saved-id', 'saved-secret')]
    retry_http = request.https[1]
    assert isinstance(retry_http, google_auth_httplib2.AuthorizedHttp)
    assert retry_http.credentials.refresh_token == 'new-refresh'
    assert gcal_with_dead_login.cal_service is None
    with (tmp_path / 'oauth').open('rb') as gcalcli_oauth:
        assert pickle.load(gcalcli_oauth).refresh_token == 'new-refresh'
    assert (tmp_path / 'oauth.bak').exists()


@pytest.mark.parametrize('answer', ['n\n', ''])
def test_invalid_grant_interactive_declined(
    gcal_with_dead_login, sign_ins, monkeypatch, tmp_path, answer
):
    monkeypatch.setattr(sys, 'stdin', FakeTTY(answer))
    monkeypatch.setattr(sys, 'stderr', FakeTTY())

    with pytest.raises(GcalcliError, match='gcalcli init'):
        gcal_with_dead_login._retry_with_backoff(
            FakeRequest(invalid_grant_error()))

    assert sign_ins == []
    with (tmp_path / 'oauth').open('rb') as gcalcli_oauth:
        assert pickle.load(gcalcli_oauth).refresh_token == 'old-refresh'


def test_other_refresh_errors_are_not_handled(gcal_with_dead_login):
    error = google.auth.exceptions.RefreshError(
        'invalid_client: Unauthorized', {'error': 'invalid_client'})

    with pytest.raises(google.auth.exceptions.RefreshError) as exc_info:
        gcal_with_dead_login._retry_with_backoff(FakeRequest(error))

    assert exc_info.value is error


def test_init_reuses_saved_client(gcal_with_dead_login, sign_ins, monkeypatch):
    # Only one answer: if init asked for a client ID too, input() would fail.
    monkeypatch.setattr(sys, 'stdin', io.StringIO('y\n'))

    gcal_with_dead_login.SetupAuth()

    assert sign_ins == [('saved-id', 'saved-secret')]


def test_cli_reports_errors_from_setup_without_traceback(
    tmp_path, monkeypatch, capsys
):
    def failing_interface(**kwargs):
        raise GcalcliError('Error: setup failed\n')

    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('GCALCLI_CONFIG', str(tmp_path))
    monkeypatch.setattr(gcalcli.cli, 'GoogleCalendarInterface',
                        failing_interface)
    monkeypatch.setattr(sys, 'argv', ['gcalcli', 'list'])

    with pytest.raises(SystemExit) as exc_info:
        gcalcli.cli.main()

    assert exc_info.value.code == 1
    assert 'Error: setup failed' in capsys.readouterr().err
