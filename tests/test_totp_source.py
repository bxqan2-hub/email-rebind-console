import pytest

from totp_source import resolve_totp_secret


def test_resolves_raw_totp_secret():
    assert resolve_totp_secret("JBSW Y3DP-EHPK3PXP") == "JBSWY3DPEHPK3PXP"


def test_resolves_2fa_url_path_secret():
    assert (
        resolve_totp_secret("https://2fa.example/JBSWY3DPEHPK3PXP")
        == "JBSWY3DPEHPK3PXP"
    )


def test_resolves_otpauth_query_secret():
    assert (
        resolve_totp_secret("otpauth://totp/account?secret=JBSWY3DPEHPK3PXP")
        == "JBSWY3DPEHPK3PXP"
    )


def test_rejects_url_without_secret():
    with pytest.raises(ValueError, match="没有可识别"):
        resolve_totp_secret("https://2fa.example/account")
