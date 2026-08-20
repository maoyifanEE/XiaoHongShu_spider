from xhs_profile_exporter.cli import exit_code_for_results


def test_collect_exit_code_reflects_run_status():
    assert exit_code_for_results("collect", {"c": {"status": "SUCCESS"}}) == 0
    assert exit_code_for_results("collect", {"c": {"status": "PARTIAL_SUCCESS_SAFE_STOP"}}) == 2
    assert exit_code_for_results("smoke", {"c": {"status": "FAILED"}}) == 2


def test_login_exit_code_uses_login_status():
    assert exit_code_for_results("login-only", {"c": {"status": "LOGIN_OK"}}) == 0
    assert exit_code_for_results("login-only", {"c": {"status": "LOGIN_EXPIRED"}}) == 2
