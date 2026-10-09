# -*- coding: utf-8 -*-
"""为已注册的 xAI 账号设置 console workspace/team。"""

from __future__ import annotations

import re
import time
from urllib.parse import parse_qs, urlsplit


CONSOLE_URL = "https://console.x.ai/"
DEFAULT_WORKSPACE_NAME = "My xAI Team"
AUTO_CLICK_DELAY_SECONDS = 1.0
CONSOLE_HOME_IMPORT_DELAY_SECONDS = 8.0
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


class WorkspaceSetupCancelled(Exception):
    """用户取消了 workspace 设置。"""


class WorkspaceSetupError(Exception):
    """console workspace 设置失败。"""


def extract_team_id(url: str) -> str:
    """从 ``/team/<UUID>`` 或 ``created=<UUID>`` 提取 team/workspace ID。"""
    text = str(url or "")
    try:
        parsed = urlsplit(text)
    except ValueError:
        parsed = None

    path = parsed.path if parsed is not None else text
    match = re.search(r"/team/([^/?#]+)", path, flags=re.IGNORECASE)
    if match:
        uuid_match = _UUID_PATTERN.fullmatch(match.group(1))
        if uuid_match:
            return uuid_match.group(0)

    if parsed is not None:
        try:
            created_values = parse_qs(parsed.query).get("created", ())
        except ValueError:
            created_values = ()
        for value in created_values:
            uuid_match = _UUID_PATTERN.fullmatch(str(value).strip())
            if uuid_match:
                return uuid_match.group(0)

    match = re.search(
        r"(?:[?&#]|^)created=([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
        r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12})(?:&|#|$)",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _sanitize(value, secret="") -> str:
    text = str(value or "")
    if secret:
        text = text.replace(str(secret), "[redacted]")
    return text


def _current_url(page_obj) -> str:
    return str(getattr(page_obj, "url", "") or "")


def _raise_if_cancelled(cancel_callback=None) -> None:
    if cancel_callback and cancel_callback():
        raise WorkspaceSetupCancelled("用户停止注册")


def _set_sso_cookies(page_obj, sso_token: str) -> None:
    """在 x.ai 与 grok.com 上设置 console 所需的 SSO cookie。"""
    token = str(sso_token or "")
    if not token:
        return
    cookies = [
        {"name": "sso", "value": token, "domain": ".x.ai", "path": "/"},
        {"name": "sso-rw", "value": token, "domain": ".x.ai", "path": "/"},
        {"name": "sso", "value": token, "domain": ".grok.com", "path": "/"},
        {"name": "sso-rw", "value": token, "domain": ".grok.com", "path": "/"},
    ]
    setter = getattr(getattr(page_obj, "set", None), "cookies", None)
    if callable(setter):
        try:
            setter(cookies)
            return
        except Exception:
            pass
    try:
        page_obj.run_js(
            """
const token = arguments[0];
document.cookie = 'sso=' + token + '; path=/; domain=.x.ai';
document.cookie = 'sso-rw=' + token + '; path=/; domain=.x.ai';
document.cookie = 'sso=' + token + '; path=/; domain=.grok.com';
document.cookie = 'sso-rw=' + token + '; path=/; domain=.grok.com';
return true;
            """,
            token,
        )
    except Exception as exc:
        raise WorkspaceSetupError("无法设置 console 会话 cookie") from exc


_WORKSPACE_FILL_SCRIPT = r"""
const workspaceName = String(arguments[0] || '').trim();
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const enabled = (node) => visible(node) && !node.disabled
  && node.getAttribute('aria-disabled') !== 'true';
const exactButton = (label) => Array.from(
  document.querySelectorAll('button, [role="button"]')
).find((node) => enabled(node)
  && String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim() === label);

const welcome = document.querySelector('#welcome-team-name');
let filled = false;
if (welcome && enabled(welcome) && !welcome.readOnly && workspaceName) {
  const prototype = welcome instanceof HTMLTextAreaElement
    ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
  if (setter) setter.call(welcome, workspaceName);
  else welcome.value = workspaceName;
  welcome.dispatchEvent(new InputEvent('input', {
    bubbles: true,
    data: workspaceName,
    inputType: 'insertText',
  }));
  welcome.dispatchEvent(new Event('change', {bubbles: true}));
  welcome.blur();
  filled = String(welcome.value || '').trim() === workspaceName;
}
return {
  url: String(location.href || ''),
  welcome_present: Boolean(welcome),
  welcome_visible: Boolean(welcome && visible(welcome)),
  welcome_editable: Boolean(welcome && enabled(welcome) && !welcome.readOnly),
  filled,
  value: String(welcome?.value || ''),
};
"""


_WORKSPACE_CLICK_SCRIPT = r"""
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const enabled = (node) => visible(node) && !node.disabled
  && node.getAttribute('aria-disabled') !== 'true';
const exactButton = (label) => Array.from(
  document.querySelectorAll('button, [role="button"]')
).find((node) => enabled(node)
  && String(node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim() === label);

let hobbyistClicked = false;
const hobbyist = exactButton('Hobbyist');
if (hobbyist) {
  hobbyist.click();
  hobbyistClicked = true;
}
let continueClicked = false;
const continueButton = exactButton('Continue');
if (continueButton) {
  continueButton.click();
  continueClicked = true;
}
return {
  url: String(location.href || ''),
  hobbyist_clicked: hobbyistClicked,
  continue_clicked: continueClicked,
};
"""


_SELECT_EXPLORE_SCRIPT = r"""
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const textOf = (node) => String(node.innerText || node.textContent || '')
  .replace(/\s+/g, ' ').trim();
const clickableAncestor = (node) => {
  let current = node;
  for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
    const radio = current.querySelector('input[type="radio"], [role="radio"]');
    if (radio && visible(radio)) return radio;
    const tag = String(current.tagName || '').toLowerCase();
    const role = String(current.getAttribute('role') || '').toLowerCase();
    const style = window.getComputedStyle(current);
    if (['button', 'a', 'label', 'input'].includes(tag)
      || ['button', 'link', 'radio', 'tab'].includes(role)
      || current.hasAttribute('tabindex')
      || typeof current.onclick === 'function'
      || style.cursor === 'pointer') {
      return current;
    }
  }
  return null;
};
const nodes = Array.from(document.querySelectorAll('body *')).filter(visible);
const findCard = (kind) => {
  const candidates = nodes.map((node) => ({ node, text: textOf(node) })).filter(({ text }) => {
    const compact = text.replace(/\s+/g, '').toLowerCase();
    if (kind === 'skip') return /^(skip|skipfornow)$/i.test(compact);
    return compact === 'explore' || (/explore/i.test(text)
      && !compact.includes('prototype') && !compact.includes('build') && text.length < 260);
  }).sort((a, b) => a.text.length - b.text.length);
  for (const item of candidates) {
    const target = clickableAncestor(item.node);
    if (target && visible(target)) return { target, text: item.text };
  }
  return null;
};
const skip = findCard('skip');
if (skip) {
  skip.target.click();
  return { found: true, clicked: true, kind: 'skip', text: skip.text, url: String(location.href || '') };
}
const explore = findCard('explore');
if (!explore) return { found: false, clicked: false, url: String(location.href || '') };
explore.target.click();
return { found: true, clicked: true, kind: 'explore', text: explore.text, url: String(location.href || '') };
""";


def _select_explore_starting_point(page_obj, deadline, log_callback=None):
    """Select the free Explore plan after a new workspace is created."""
    wait_deadline = min(deadline, time.monotonic() + 20.0)
    while time.monotonic() < wait_deadline:
        raw_page = getattr(page_obj, "raw_page", None)
        if raw_page is not None:
            try:
                skip = raw_page.get_by_role("button", name="Skip", exact=True).first
                if not skip.count():
                    skip = raw_page.get_by_role("link", name="Skip", exact=True).first
                if skip.count() and skip.is_visible():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    skip.click(timeout=3000)
                    if log_callback:
                        log_callback("[workspace] 检测到付费方案页，已选择 Skip，未点击 Checkout")
                    return "skip"
                explore_text = raw_page.get_by_text("Explore", exact=True).first
                if explore_text.count() and explore_text.is_visible():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    explore_text.click(timeout=3000)
                    if log_callback:
                        log_callback("[workspace] 已选择 Explore（免费方案），未点击 Checkout")
                    return "explore"
                native_radios = raw_page.locator('input[type="radio"]')
                if native_radios.count():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    native_radios.first.check(timeout=3000, force=True)
                    if log_callback:
                        log_callback("[workspace] 已勾选 Explore 原生单选项，未点击 Checkout")
                    return "explore"
                explore_radio = raw_page.get_by_role("radio", name="Explore", exact=True).first
                if explore_radio.count() and explore_radio.is_visible():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    explore_radio.click(timeout=3000)
                    if log_callback:
                        log_callback("[workspace] 已点击 Explore 单选项，未点击 Checkout")
                    return "explore"
                explore = raw_page.get_by_role("button", name="Explore", exact=True).first
                if not explore.count():
                    explore = raw_page.get_by_role("link", name="Explore", exact=True).first
                if explore.count() and explore.is_visible():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    explore.click(timeout=3000)
                    if log_callback:
                        log_callback("[workspace] 已选择 Explore（免费方案），未点击 Checkout")
                    return "explore"
            except Exception:
                pass
        # Prefer a real Playwright click through the shared page adapter. This
        # produces the same event sequence as a user clicking the card.
        if hasattr(page_obj, "ele"):
            try:
                # Adapter text locators are only a fallback; the JS branch
                # below restricts clicks to actual interactive elements.
                skip = page_obj.ele("tag:button")
                if skip.states.is_displayed and re.search(
                    r"^skip(?: for now)?$", str(getattr(skip, "text", "") or ""), re.I
                ):
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    skip.click(timeout=3)
                    if log_callback:
                        log_callback("[workspace] 检测到付费方案页，已选择 Skip，未点击 Checkout")
                    return "skip"
            except Exception:
                pass
        try:
            time.sleep(AUTO_CLICK_DELAY_SECONDS)
            state = page_obj.run_js(_SELECT_EXPLORE_SCRIPT)
        except Exception:
            state = {}
        if isinstance(state, dict) and state.get("clicked"):
            if log_callback:
                if state.get("kind") == "skip":
                    log_callback("[workspace] 检测到付费方案页，已选择 Skip，未点击 Checkout")
                else:
                    log_callback("[workspace] 已选择 Explore（免费方案），未点击 Checkout")
            return state.get("kind") or "explore"
        time.sleep(min(0.25, max(wait_deadline - time.monotonic(), 0)))
    return False


def _select_explore_and_pause(page_obj, deadline, pause_seconds=0, log_callback=None) -> bool:
    selection = _select_explore_starting_point(page_obj, deadline, log_callback=log_callback)
    pause = max(float(pause_seconds or 0), 0.0)
    if log_callback:
        if selection:
            label = "Skip" if selection == "skip" else "Explore"
            log_callback(f"[workspace] {label} 已选择，停留 {pause:.0f}s 后继续处理方案按钮")
        else:
            log_callback(f"[workspace] 未确认方案按钮，停留 {pause:.0f}s 等待手动选择，暂不进入 CPA OAuth")
    if pause:
        # This pause is intentionally independent of the workspace navigation
        # deadline; slow Console redirects must not remove the debug window.
        time.sleep(pause)
    if selection == "skip":
        plan_complete = _finish_api_key_screen(page_obj, log_callback=log_callback)
    else:
        plan_complete = _click_plan_continue(page_obj, deadline, log_callback=log_callback)
    if not plan_complete:
        raise WorkspaceSetupError("方案页面操作未确认，已停止后续 OAuth")
    if pause:
        if log_callback:
            log_callback(f"[workspace] 方案/API Key 页面处理完成，停留 {pause:.0f}s 后继续 CPA OAuth")
        time.sleep(pause)
    return bool(selection)


def _finish_api_key_screen(page_obj, log_callback=None, allow_direct_skip=False) -> bool:
    """Leave the one-time API-key screen without copying the key."""
    deadline = time.monotonic() + 30.0
    get_started_done = False
    leave_anyway_done = False
    skip_for_now_done = False
    while time.monotonic() < deadline:
        # The Console home can already be mounted behind the onboarding
        # overlay.  Once the page has no visible plan/onboarding control and
        # exposes the real Create API key action, it is safe to continue even
        # when a product announcement modal covers the dashboard.  The
        # dashboard detector explicitly rejects visible plan controls, so
        # this does not reintroduce the old Continue/Skip false positive.
        if _console_dashboard_ready(page_obj):
            if log_callback:
                log_callback("[workspace] 检测到 Create API key，8s 后开始 CPA OAuth")
            time.sleep(CONSOLE_HOME_IMPORT_DELAY_SECONDS)
            return True
        raw_page = getattr(page_obj, "raw_page", None)
        if raw_page is not None:
            try:
                if not get_started_done:
                    get_started = raw_page.get_by_role("button", name="Get started", exact=True).first
                    if get_started.count() and get_started.is_visible():
                        time.sleep(AUTO_CLICK_DELAY_SECONDS)
                        get_started.click(timeout=3000)
                        get_started_done = True
                        if log_callback:
                            log_callback("[workspace] 已点击 Get started")
                        time.sleep(0.5)
                        continue
                    if allow_direct_skip:
                        skip_for_now_done = _click_skip_for_now(
                            page_obj,
                            log_callback=log_callback,
                        )
                        if skip_for_now_done:
                            continue
                elif not leave_anyway_done:
                    leave = raw_page.get_by_role("button", name="Leave anyway", exact=True).first
                    if leave.count() and leave.is_visible():
                        time.sleep(AUTO_CLICK_DELAY_SECONDS)
                        leave.click(timeout=3000)
                        leave_anyway_done = True
                        if log_callback:
                            log_callback("[workspace] 已点击 Leave anyway，未复制 API Key")
                        time.sleep(0.5)
                        continue
                elif not skip_for_now_done:
                    skip_for_now_done = _click_skip_for_now(
                        page_obj,
                        log_callback=log_callback,
                    )
                    if skip_for_now_done:
                        continue
            except Exception:
                pass
        if (
            allow_direct_skip
            and not get_started_done
            and not leave_anyway_done
            and not skip_for_now_done
            and raw_page is None
        ):
            try:
                skip_for_now_done = _click_skip_for_now(
                    page_obj,
                    log_callback=log_callback,
                )
                if skip_for_now_done:
                    continue
            except Exception:
                pass
        try:
            if skip_for_now_done:
                action = ""
            elif leave_anyway_done:
                action = "skip for now"
            else:
                action = "leave anyway" if get_started_done else "get started"
            if not action:
                time.sleep(0.25)
                continue
            result = page_obj.run_js(
                """
const action = arguments[0];
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const buttons = Array.from(document.querySelectorAll(
  'button, [role="button"], a, input[type="button"], input[type="submit"]'
));
const label = (node) => String(node.innerText || node.textContent || node.value || '')
  .replace(/\s+/g, ' ').trim().toLowerCase();
const target = buttons.find((node) => visible(node) && label(node) === action);
if (!target) return false;
target.click();
return true;
                """,
                action,
            )
            if result:
                if action == "get started":
                    get_started_done = True
                    if log_callback:
                        log_callback("[workspace] 已点击 Get started")
                    time.sleep(0.5)
                    continue
                if action == "leave anyway":
                    leave_anyway_done = True
                    if log_callback:
                        log_callback("[workspace] 已点击 Leave anyway，未复制 API Key")
                    time.sleep(0.5)
                    continue
                if action == "skip for now":
                    skip_for_now_done = True
                    if log_callback:
                        log_callback("[workspace] 已点击 Skip for now")
                    time.sleep(0.5)
                    continue
        except Exception:
            pass
        time.sleep(0.25)
    if log_callback:
        missing = (
            "Get started"
            if not get_started_done
            else "Leave anyway"
            if not leave_anyway_done
            else "Skip for now"
        )
        log_callback(f"[workspace] API Key 引导未完成，未确认 {missing}，停止 OAuth")
    return False


def _console_dashboard_ready(page_obj) -> bool:
    """Recognize the completed Console home screen after onboarding."""
    try:
        state = page_obj.run_js(
            """
const body = String(document.body && (document.body.innerText || document.body.textContent) || '')
  .replace(/\s+/g, ' ').trim();
const hasWelcome = /\bWelcome,\s+[^,]{1,80}/i.test(body);
const hasDashboard = /\bDashboard\b/i.test(body);
const controls = Array.from(document.querySelectorAll(
  'button, [role="button"], a, input[type="button"], input[type="submit"]'
));
const label = (node) => String(node.innerText || node.textContent || node.value || '')
  .replace(/\s+/g, ' ').trim();
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const labels = controls.filter(visible).map(label);
const normalizedLabels = labels.map((text) => text.replace(/^[^A-Za-z0-9]+/, '').trim());
const hasApiKeyAction = normalizedLabels.some((text) => /^(Create API key|API Keys)$/i.test(text));
const hasPlanControl = labels.some((text) => /^(Explore|Skip|Skip for now|Continue for free|Checkout|Get started|Leave anyway)$/i.test(text));
return {
  ready: hasWelcome && hasDashboard && hasApiKeyAction && !hasPlanControl,
  url: String(location.href || ''),
};
            """
        )
        return isinstance(state, dict) and bool(state.get("ready"))
    except Exception:
        return False


def _click_skip_for_now(page_obj, log_callback=None) -> bool:
    """Dismiss the post-Explore feature tour without entering a feature."""
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        raw_page = getattr(page_obj, "raw_page", None)
        if raw_page is not None:
            try:
                text_skip = raw_page.get_by_text("Skip for now", exact=True).first
                if text_skip.count() and text_skip.is_visible():
                    box = text_skip.bounding_box()
                    if box:
                        time.sleep(AUTO_CLICK_DELAY_SECONDS)
                        raw_page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        if log_callback:
                            log_callback("[workspace] 已用真实鼠标点击 Skip for now")
                        return True
                if _real_mouse_click_text(raw_page, "Skip for now"):
                    if log_callback:
                        log_callback("[workspace] 已用真实鼠标点击 Skip for now")
                    return True
                skip = raw_page.get_by_role("button", name="Skip for now", exact=True).first
                if not skip.count():
                    skip = raw_page.get_by_role("link", name="Skip for now", exact=True).first
                if skip.count() and skip.is_visible():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    skip.click(timeout=3000)
                    if log_callback:
                        log_callback("[workspace] 已点击 Skip for now")
                    return True
            except Exception:
                pass
        try:
            result = page_obj.run_js(
                """
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const nodes = Array.from(document.querySelectorAll(
  'button, [role="button"], a, input[type="button"], input[type="submit"]'
));
const target = nodes.find((node) => visible(node)
  && String(node.innerText || node.textContent || node.value || '')
    .replace(/\s+/g, ' ').trim().toLowerCase() === 'skip for now');
if (!target) return false;
target.click();
return true;
                """
            )
            if result:
                if log_callback:
                    log_callback("[workspace] 已点击 Skip for now")
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _real_mouse_click_text(raw_page, target_text: str) -> bool:
    """Click an exact visible button label using the page's real mouse."""
    normalized_target = re.sub(r"\s+", " ", target_text).strip().lower()
    for selector in (
        "button",
        '[role="button"]',
        "a",
        'input[type="button"]',
        'input[type="submit"]',
    ):
        try:
            locator = raw_page.locator(selector)
            for index in range(locator.count()):
                item = locator.nth(index)
                if not item.is_visible():
                    continue
                text = re.sub(r"\s+", " ", item.inner_text()).strip().lower()
                if text != normalized_target:
                    continue
                box = item.bounding_box()
                if not box:
                    continue
                time.sleep(AUTO_CLICK_DELAY_SECONDS)
                raw_page.mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
                return True
        except Exception:
            continue
    return False


def _click_plan_continue(page_obj, deadline, log_callback=None) -> bool:
    """Submit the selected free plan without ever clicking Checkout."""
    wait_deadline = max(time.monotonic(), deadline)
    wait_deadline = min(wait_deadline + 8.0, time.monotonic() + 8.0)
    while time.monotonic() < wait_deadline:
        raw_page = getattr(page_obj, "raw_page", None)
        if raw_page is not None:
            try:
                if _real_mouse_click_text(raw_page, "Continue for free"):
                    if log_callback:
                        log_callback("[workspace] 已用真实鼠标点击 Continue for free，未点击 Checkout")
                    if _finish_api_key_screen(
                        page_obj,
                        log_callback=log_callback,
                        allow_direct_skip=True,
                    ):
                        return True
                    if log_callback:
                        log_callback("[workspace] API Key 后续页面未确认，停止 OAuth")
                    return False
                text_button = raw_page.get_by_text("Continue for free", exact=True).first
                if text_button.count() and text_button.is_visible():
                    box = text_button.bounding_box()
                    if box:
                        time.sleep(AUTO_CLICK_DELAY_SECONDS)
                        raw_page.mouse.click(
                            box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2,
                        )
                        if log_callback:
                            log_callback("[workspace] 已用真实鼠标点击 Continue for free，未点击 Checkout")
                        if _finish_api_key_screen(
                            page_obj,
                            log_callback=log_callback,
                            allow_direct_skip=True,
                        ):
                            return True
                        if log_callback:
                            log_callback("[workspace] API Key 后续页面未确认，停止 OAuth")
                        return False
                button = raw_page.get_by_role("button", name="Continue for free", exact=True).first
                if not button.count():
                    button = raw_page.get_by_text("Continue for free", exact=True).first
                if button.count() and button.is_visible():
                    time.sleep(AUTO_CLICK_DELAY_SECONDS)
                    button.click(timeout=3000)
                    if log_callback:
                        log_callback("[workspace] 已点击 Continue for free，未点击 Checkout")
                    if _finish_api_key_screen(
                        page_obj,
                        log_callback=log_callback,
                        allow_direct_skip=True,
                    ):
                        return True
                    if log_callback:
                        log_callback("[workspace] API Key 后续页面未确认，停止 OAuth")
                        return False
            except Exception:
                pass
        try:
            result = page_obj.run_js(
                """
const visible = (node) => {
  if (!node) return false;
  const style = window.getComputedStyle(node);
  const rect = node.getBoundingClientRect();
  return style.display !== 'none' && style.visibility !== 'hidden'
    && style.opacity !== '0' && rect.width > 0 && rect.height > 0;
};
const clickableAncestor = (node) => {
  let current = node;
  for (let depth = 0; current && depth < 7; depth += 1, current = current.parentElement) {
    const radio = current.querySelector('input[type="radio"], [role="radio"]');
    if (radio && visible(radio)) return radio;
    const tag = String(current.tagName || '').toLowerCase();
    const role = String(current.getAttribute('role') || '').toLowerCase();
    const style = window.getComputedStyle(current);
    if (['button', 'a', 'label', 'input'].includes(tag)
      || ['button', 'link', 'tab'].includes(role)
      || current.hasAttribute('tabindex')
      || typeof current.onclick === 'function'
      || style.cursor === 'pointer') return current;
  }
  return null;
};
const candidates = Array.from(document.querySelectorAll('body *'))
  .filter(visible)
  .filter((node) => String(node.innerText || node.textContent || '')
    .replace(/\s+/g, ' ').trim().toLowerCase() === 'continue for free');
let target = null;
for (const node of candidates) {
  target = clickableAncestor(node);
  if (target && visible(target)) break;
}
if (!target) return false;
target.click();
return true;
                """
            )
            if result:
                if log_callback:
                    log_callback("[workspace] 已点击 Continue for free，未点击 Checkout")
                if _finish_api_key_screen(
                    page_obj,
                    log_callback=log_callback,
                    allow_direct_skip=True,
                ):
                    return True
                if log_callback:
                    log_callback("[workspace] API Key 后续页面未确认，停止 OAuth")
                return False
        except Exception:
            pass
        time.sleep(0.25)
    return False


def _native_fill_workspace_name(page_obj, workspace_name: str):
    """Use the adapter's trusted keyboard path when available."""
    if not hasattr(page_obj, "ele"):
        return None
    try:
        field = page_obj.ele("#welcome-team-name")
        if not field.states.is_displayed:
            return {"welcome_present": False, "filled": False, "value": ""}
        field.input(workspace_name, clear=True)
        value = str(field.property("value") or "").strip()
        return {
            "welcome_present": True,
            "welcome_visible": True,
            "welcome_editable": bool(field.states.is_enabled),
            "filled": value == workspace_name,
            "value": value,
        }
    except Exception:
        return {"welcome_present": True, "filled": False, "value": ""}


def _run_workspace_action(page_obj, workspace_name: str) -> dict:
    result = {}
    native_state = _native_fill_workspace_name(page_obj, workspace_name)
    if native_state is None:
        native_state = page_obj.run_js(_WORKSPACE_FILL_SCRIPT, workspace_name)
    if isinstance(native_state, dict):
        result.update(native_state)

    # Do not submit an empty/unsynchronised controlled input. The previous
    # implementation could navigate to /welcome?name= before React committed
    # the value, which looked like a successful click but created no workspace.
    if not result.get("filled"):
        result.update({"hobbyist_clicked": False, "continue_clicked": False})
        return result

    time.sleep(0.35)
    time.sleep(AUTO_CLICK_DELAY_SECONDS)
    clicked = page_obj.run_js(_WORKSPACE_CLICK_SCRIPT)
    if isinstance(clicked, dict):
        result.update(clicked)
    return result


def _wait_for_team_url(page_obj, deadline, cancel_callback=None, max_seconds=2.0):
    """Give the Continue click time to finish its client-side redirect."""
    wait_deadline = min(deadline, time.monotonic() + max(float(max_seconds), 0.0))
    while time.monotonic() < wait_deadline:
        _raise_if_cancelled(cancel_callback)
        current_url = _current_url(page_obj)
        team_id = extract_team_id(current_url)
        if team_id:
            return team_id, current_url
        time.sleep(min(0.2, max(wait_deadline - time.monotonic(), 0)))
    current_url = _current_url(page_obj)
    return extract_team_id(current_url), current_url


def _is_console_url(url: str) -> bool:
    try:
        host = (urlsplit(str(url or "")).hostname or "").lower()
    except ValueError:
        return False
    return host == "console.x.ai" or host.endswith(".console.x.ai")


def _wait_for_page_stable(page_obj, deadline, cancel_callback=None, max_seconds=3.0):
    """Wait until a navigation has a usable JS execution context."""
    wait_deadline = min(deadline, time.monotonic() + max(float(max_seconds), 0.0))
    while time.monotonic() < wait_deadline:
        _raise_if_cancelled(cancel_callback)
        try:
            state = page_obj.run_js(
                "return { url: String(location.href || ''), readyState: document.readyState };"
            )
            if isinstance(state, dict) and state.get("readyState") in {"interactive", "complete"}:
                return state
        except Exception:
            # Playwright can briefly destroy the old execution context while a
            # redirect installs the new document. Retry after the next tick.
            pass
        time.sleep(min(0.2, max(wait_deadline - time.monotonic(), 0)))
    return {}


def _wait_doc_loaded(page_obj) -> None:
    try:
        page_obj.wait.doc_loaded()
    except Exception:
        pass


def _navigate_console(page_obj, timeout_seconds: float, log_callback=None) -> None:
    started = time.monotonic()
    last_error = ""
    for attempt in range(2):
        remaining = max(float(timeout_seconds) - (time.monotonic() - started), 0.5)
        try:
            page_obj.get(
                CONSOLE_URL,
                wait_until="domcontentloaded" if attempt == 0 else "commit",
                timeout=max(1, int(remaining * 1000)),
            )
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {_sanitize(exc)[:180]}"
            if log_callback and attempt == 0:
                log_callback(f"[workspace] console 导航等待异常，重试导航: {last_error}")
        _wait_doc_loaded(page_obj)
        _wait_for_page_stable(page_obj, started + max(float(timeout_seconds), 0.5))
        if _is_console_url(_current_url(page_obj)) or extract_team_id(_current_url(page_obj)):
            return
    if log_callback and last_error:
        log_callback(f"[workspace] console 导航未稳定: {last_error}")


def _result(status, team_id, workspace_name, url, error="") -> dict:
    return {
        "status": status,
        "team_id": team_id,
        "workspace_name": workspace_name,
        "url": url,
        "error": error,
    }


def setup_workspace(
    page_obj,
    workspace_name,
    sso_token="",
    timeout=60,
    explore_pause_seconds=0,
    log_callback=None,
    cancel_callback=None,
) -> dict:
    """同步完成 console workspace 设置，并返回可持久化的摘要。"""
    name = str(workspace_name or DEFAULT_WORKSPACE_NAME).strip() or DEFAULT_WORKSPACE_NAME
    token = str(sso_token or "")
    timeout_seconds = max(float(timeout or 0), 0.1)
    initial_url = _current_url(page_obj) if page_obj is not None else ""
    initial_team_id = extract_team_id(initial_url)
    if initial_team_id:
        return _result(
            "already_configured",
            initial_team_id,
            name,
            _sanitize(initial_url, token),
        )

    if page_obj is None:
        error = "WorkspaceSetupError: 浏览器页面未就绪，无法设置 workspace"
        return _result("failed", "", name, "", error)

    deadline = time.monotonic() + timeout_seconds
    try:
        _raise_if_cancelled(cancel_callback)
        _set_sso_cookies(page_obj, token)

        # 第一轮从 console 根地址开始；浏览器可能会把它重定向到欢迎页或 team 页。
        _navigate_console(page_obj, timeout_seconds, log_callback=log_callback)
        fallback_used = False
        while time.monotonic() < deadline:
            _raise_if_cancelled(cancel_callback)
            current_url = _current_url(page_obj)
            team_id = extract_team_id(current_url)
            if team_id:
                status = "created" if fallback_used or initial_url else "already_configured"
                if status == "created":
                    _select_explore_and_pause(
                        page_obj,
                        deadline,
                        pause_seconds=explore_pause_seconds,
                        log_callback=log_callback,
                    )
                return _result(status, team_id, name, _sanitize(current_url, token))

            try:
                state = _run_workspace_action(page_obj, name)
            except Exception as exc:
                error_text = str(exc).lower()
                if "execution context was destroyed" not in error_text and "context was destroyed" not in error_text:
                    raise
                if log_callback:
                    log_callback("[workspace] 页面仍在跳转，等待执行上下文稳定后重试")
                _wait_for_page_stable(page_obj, deadline, cancel_callback=cancel_callback)
                continue
            state_url = str(state.get("url") or "")
            current_url = state_url or _current_url(page_obj)
            team_id = extract_team_id(current_url)
            if team_id:
                _select_explore_and_pause(
                    page_obj,
                    deadline,
                    pause_seconds=explore_pause_seconds,
                    log_callback=log_callback,
                )
                return _result("created", team_id, name, _sanitize(current_url, token))
            if state.get("continue_clicked"):
                team_id, current_url = _wait_for_team_url(
                    page_obj,
                    deadline,
                    cancel_callback=cancel_callback,
                )
                if team_id:
                    _select_explore_and_pause(
                        page_obj,
                        deadline,
                        pause_seconds=explore_pause_seconds,
                        log_callback=log_callback,
                    )
                    return _result("created", team_id, name, _sanitize(current_url, token))

            # 点击后没有及时推进时，重新访问根地址，让 console 重新解析会话和 workspace。
            if not fallback_used:
                fallback_used = True
                _navigate_console(
                    page_obj,
                    min(timeout_seconds, max(1, deadline - time.monotonic())),
                    log_callback=log_callback,
                )
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.25, remaining))

        current_url = _current_url(page_obj)
        message = (
            "WorkspaceSetupError: 未找到 workspace/team ID；"
            f"当前 URL: {_sanitize(current_url, token) or 'empty'}"
        )
        return _result("failed", "", name, _sanitize(current_url, token), message)
    except WorkspaceSetupCancelled:
        raise
    except WorkspaceSetupError as exc:
        message = f"WorkspaceSetupError: {_sanitize(exc, token)}"
        return _result("failed", "", name, _sanitize(_current_url(page_obj), token), message)
    except Exception as exc:
        message = f"WorkspaceSetupError: {_sanitize(type(exc).__name__, token)}: {_sanitize(exc, token)}"
        return _result("failed", "", name, _sanitize(_current_url(page_obj), token), message)
