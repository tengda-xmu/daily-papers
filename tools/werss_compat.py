"""Bounded compatibility fixes for the pinned, private WeRSS installation."""
from pathlib import Path


def replace_checked(text: str, old: str, new: str) -> str:
    if old not in text and new not in text:
        raise RuntimeError("Unsupported WeRSS version; review connector compatibility")
    return text.replace(old, new, 1)


def prepare(root: Path):
    path = root / "core/wx/model/app.py"
    text = path.read_text(encoding="utf-8")
    marker = 'logger.warning(f"公众号[{Mps_title}]第{i+1}页请求被微信限频(freq control, ret=200013)，本次抓取中止，请稍后重试")'
    error_callback = '\n                    super().Error("frequencey control, stop at {}".format(str(begin)))'
    old = marker + error_callback + "\n                    break"
    new = marker + error_callback + '\n                    raise RuntimeError("WeChat rate limited (200013)")'
    if old not in text and new not in text:
        raise RuntimeError("Unsupported WeRSS version; verify its rate-limit handling before collecting")
    changed = text.replace(old, new).replace("verify=False", "verify=True")
    changed = changed.replace("time.sleep(random.randint(0,interval))", "time.sleep(max(30, interval))")
    if changed != text:
        path.write_text(changed, encoding="utf-8")
    api = root / "apis/mps.py"
    original = api.read_text(encoding="utf-8")
    changed = original.replace("thread.join(timeout=30)", "thread.join(timeout=150)")
    changed = replace_checked(changed,
        '    mp_intro: str = Body(None, max_length=255),\n    current_user:',
        '    mp_intro: str = Body(None, max_length=255),\n    fetch_articles: bool = Body(True),\n    current_user:')
    changed = replace_checked(changed,
        '        if not existing_feed:\n            from core.queue import TaskQueue',
        '        if not existing_feed and fetch_articles:\n            from core.queue import TaskQueue')
    changed = replace_checked(changed,
        'message=f"搜索公众号失败,请重新扫码授权！",',
        'message=f"搜索公众号失败,请检查授权或限频状态",\n                data={"error": str(e)},')
    if changed != original:
        api.write_text(changed, encoding="utf-8")
    # Upstream restores a valid session but only sets _islogin on the QR path.
    # Set the flag after an authenticated account lookup succeeds, so the UI
    # and connector report the same verified state following a restart.
    driver = root / "driver/wx_api.py"
    original = driver.read_text(encoding="utf-8")
    old = 'if self._get_account_info() is not None:\n                logger.info("登录成功！")'
    new = 'if self._get_account_info() is not None:\n                self._islogin = True\n                logger.info("登录成功！")'
    if old not in original and new not in original:
        raise RuntimeError("Unsupported WeRSS version; verify its session restoration")
    changed = original.replace(old, new)
    if changed != original:
        driver.write_text(changed, encoding="utf-8")
    base = root / "core/wx/base.py"
    original = base.read_text(encoding="utf-8")
    changed = replace_checked(original,
        'self.Error("frequencey control, stop at {}".format(str(kw)))\n                return',
        'self.Error("frequencey control, stop at {}".format(str(kw)))\n                raise RuntimeError("WeChat search rate limited (200013)")')
    changed = replace_checked(changed,
        'self.Error("错误原因:{}:代码:{}".format(msg[\'base_resp\'][\'err_msg\'],msg[\'base_resp\'][\'ret\']),code="Invalid Session")\n                return ',
        'raise RuntimeError("Invalid Session: WeChat account search requires authorization")')
    if changed != original:
        base.write_text(changed, encoding="utf-8")
