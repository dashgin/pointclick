import re
import time

import pytest

pytestmark = pytest.mark.anyio


def idx(table, pattern):
    for line in table.split("\ntext:")[0].splitlines():
        m = re.match(r"\[(\d+)\] ", line)
        if m and re.search(pattern, line):
            return m.group(1)
    raise AssertionError(f"{pattern!r} not in table:\n{table}")


async def text_of(s, selector="#out"):
    return (await s.evaluate(f"() => document.querySelector('{selector}').textContent")).strip('"')


# --- table ---


async def test_table_lists_controls_with_roles_and_ops(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    assert t.startswith(f"url: {origins[0]}/basic.html\ntitle: Basic")
    assert re.search(r"\[\d+\] button 'Click me' <CLICK>", t)
    assert re.search(r"\[\d+\] textbox 'Name' <TYPE,CLICK>", t)
    assert re.search(r"\[\d+\] combobox 'Color' value='Red' <SELECT>", t)
    assert re.search(r"    \[\d+:1\] Green", t)
    assert "\ntext:\n" in t


async def test_table_skips_disabled_hidden_password_file_and_aria_hidden(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    for label in ("Disabled", "Hidden item", "Secret", "File", "Aria hidden"):
        assert label not in t.split("\ntext:")[0], label


async def test_viewport_only_by_default_full_on_request(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    assert "Bottom button" not in t
    assert "(more below:" in t
    full = await browser.observe(full=True)
    assert "Bottom button" in full
    assert "(more below:" not in full


# --- act by index ---


async def test_click(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("CLICK", idx(t, "Click me"))
    assert "clicked" in t
    assert await text_of(browser) == "clicked"


async def test_type_shows_value(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("TYPE", idx(t, "'Name'"), "Ada")
    assert re.search(r"textbox 'Name' value='Ada'", t)


async def test_type_text_alias(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("type_text", idx(t, "'Name'"), "Bo")
    assert "value='Bo'" in t


async def test_select_by_option_index(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("SELECT", f"{idx(t, 'Color')}:2")  # Green, Blue are the unselected options
    assert "value='Blue'" in t
    assert await text_of(browser) == "color b"


async def test_select_by_label(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("SELECT", idx(t, "Color"), "Green")
    assert await text_of(browser) == "color g"


async def test_press_on_target_submits(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    q = idx(t, "Query")
    await browser.act("TYPE", q, "cats")
    await browser.act("PRESS", q, "Enter")
    assert await text_of(browser) == "submitted cats"


async def test_press_without_target_uses_focus(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    await browser.act("TYPE", idx(t, "Query"), "dogs")
    await browser.act("PRESS", "", "Enter")
    assert await text_of(browser) == "submitted dogs"


async def test_hover_reveals_hidden_item(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("HOVER", "#menu")
    t = await browser.act("CLICK", idx(t, "Hidden item"))
    assert await text_of(browser) == "menu"


async def test_scroll_reveals_below_fold(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("SCROLL_DOWN")
    for _ in range(5):
        if "Bottom button" in t:
            break
        t = await browser.act("SCROLL_DOWN")
    await browser.act("CLICK", idx(t, "Bottom button"))
    assert await text_of(browser) == "bottom"
    t = await browser.act("SCROLL_UP")
    assert int(await browser.evaluate("() => scrollY")) < 2600


async def test_click_off_screen_index_from_full_observe(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    full = await browser.observe(full=True)
    await browser.act("CLICK", idx(full, "Bottom button"))
    assert await text_of(browser) == "bottom"


# --- WAIT ---


async def test_wait_returns_early_on_change(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("CLICK", idx(t, "Load later"))
    assert "Late button" not in t
    t0 = time.perf_counter()
    t = await browser.act("WAIT")
    assert "Late button" in t
    assert time.perf_counter() - t0 < 1.5


async def test_wait_caps_when_nothing_changes(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    t0 = time.perf_counter()
    await browser.act("WAIT")
    assert 2.8 < time.perf_counter() - t0 < 4


# --- selector fallback ---


async def test_selector_reaches_password_field(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    await browser.act("TYPE", "#pw", "hunter2")
    assert await browser.evaluate("() => document.querySelector('#pw').value") == '"hunter2"'


async def test_text_selector_click(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    await browser.act("CLICK", "text=Click me")
    assert await text_of(browser) == "clicked"


async def test_selector_select_by_label(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    await browser.act("SELECT", "#color", "Blue")
    assert await text_of(browser) == "color b"


# --- errors ---


async def test_unknown_index_is_stale_with_fresh_table(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("CLICK", "999")
    assert t.startswith("Stale: No element [999]")
    assert "Click me" in t


async def test_removed_element_is_stale(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    i = idx(t, "Remove me")
    await browser.evaluate(
        "() => [...document.querySelectorAll('button')].find(b => b.textContent === 'Remove me').remove()"
    )
    t = await browser.act("CLICK", i)
    assert t.startswith(f"Stale: [{i}] is gone")


async def test_unknown_operation(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("DANCE", idx(t, "Click me"))
    assert t.startswith("Error: Unknown operation 'DANCE'")


async def test_missing_target(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("CLICK")
    assert t.startswith("Error: CLICK needs a target.")


async def test_failed_action_returns_table_not_crash(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("CLICK", "text=No such thing")
    assert t.startswith("Failed:")
    assert "Click me" in t


# --- frames ---


async def test_same_origin_iframe(browser, origins):
    t = await browser.navigate(f"{origins[0]}/frames.html?child={origins[1]}/child.html")
    assert "-- frame about:srcdoc --" in t
    t = await browser.act("CLICK", idx(t, "Same-origin button"))
    assert "same clicked" in t


async def test_cross_origin_iframe(browser, origins):
    t = await browser.navigate(f"{origins[0]}/frames.html?child={origins[1]}/child.html")
    assert f"-- frame {origins[1]}/child.html --" in t
    t = await browser.act("CLICK", idx(t, "Cross-origin button"))
    assert "cross clicked" in t
    t = await browser.act("TYPE", idx(t, "Cross input"), "hi")
    assert "Cross input' value='hi'" in t


# --- popups ---


async def test_popup_becomes_current_then_falls_back(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    t = await browser.act("CLICK", idx(t, "Open popup"))
    if "title: Popup" not in t:
        t = await browser.act("WAIT")
    assert "title: Popup" in t
    t = await browser.act("CLICK", idx(t, "Close popup"))
    assert "title: Basic" in await browser.observe()


# --- other tools ---


async def test_upload(browser, origins, tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("x")
    await browser.navigate(f"{origins[0]}/basic.html")
    await browser.upload("#file", [str(f)])
    assert await text_of(browser) == "file note.txt"


async def test_screenshot_is_jpeg(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    img = await browser.screenshot()
    assert img.data[:3] == b"\xff\xd8\xff"
    full = await browser.screenshot(full_page=True)
    assert len(full.data) > len(img.data)


async def test_evaluate_json(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    assert await browser.evaluate("() => ({a: 1, b: [2]})") == '{"a": 1, "b": [2]}'
    assert await browser.evaluate("document.title") == '"Basic"'


async def test_console_log_error_and_clear(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    await browser.act("CLICK", idx(t, "Log and throw"))
    await browser.act("WAIT")
    c = await browser.console()
    assert "[log] hello log" in c
    assert "[pageerror] boom" in c
    assert await browser.console() == "(empty)"


async def test_close_then_fresh_session(browser, origins):
    t = await browser.navigate(f"{origins[0]}/basic.html")
    await browser.act("TYPE", idx(t, "'Name'"), "Ada")
    assert await browser.close() == "closed"
    t = await browser.navigate(f"{origins[0]}/basic.html")
    assert "value='Ada'" not in t


async def test_navigate_again_reuses_browser(browser, origins):
    await browser.navigate(f"{origins[0]}/basic.html")
    b = browser.S["browser"]
    await browser.navigate(f"{origins[0]}/popup.html")
    assert browser.S["browser"] is b


# --- real-world rough edges ---


async def test_replaced_element_with_unique_label_is_followed(browser, origins):
    t = await browser.navigate(f"{origins[0]}/extra.html")
    i = idx(t, "Unique action")
    # Frameworks re-render: same label, new node.
    await browser.evaluate(
        "() => { swap.innerHTML = ''; const b = document.createElement('button'); b.textContent = 'Unique action';"
        " b.onclick = () => out.textContent = 'new'; swap.append(b); }"
    )
    t = await browser.act("CLICK", i)
    assert t.startswith("Note: The element was re-rendered; used [")
    assert await text_of(browser) == "new"


async def test_replaced_element_with_ambiguous_label_stays_stale(browser, origins):
    t = await browser.navigate(f"{origins[0]}/extra.html")
    i = idx(t, "'Start'")
    await browser.evaluate(
        "() => { const b = document.querySelector('.dup'); const c = b.cloneNode(true); b.replaceWith(c); }"
    )
    t = await browser.act("CLICK", i)
    assert t.startswith(f"Stale: [{i}] is gone")
    assert await text_of(browser) == "idle"


async def test_control_repeating_its_wrapper_label_is_listed_once(browser, origins):
    t = await browser.navigate(f"{origins[0]}/extra.html")
    assert len(re.findall(r"Nested label", t.split("\ntext:")[0])) == 1


async def test_value_only_on_fields(browser, origins):
    t = await browser.navigate(f"{origins[0]}/extra.html")
    assert re.search(r"button 'Toggle' <CLICK>", t)


async def test_labels_collapse_whitespace(browser, origins):
    t = await browser.navigate(f"{origins[0]}/extra.html")
    assert "link 'Spaced out label'" in t


async def test_timeout_after_page_changed_says_check_before_retry(browser, origins):
    t = await browser.navigate(f"{origins[0]}/extra.html")
    await browser.act("CLICK", idx(t, "Tick later"))
    t = await browser.act("CLICK", "#covered")  # never clickable; the tick lands meanwhile
    assert t.startswith("Note: The action timed out, but the page changed since.")
    assert await text_of(browser) == "ticked"


async def test_click_that_navigates_returns_fast(browser, origins):
    t = await browser.navigate(f"{origins[0]}/popup.html")
    await browser.evaluate(
        "() => { const a = document.createElement('a'); a.href = 'basic.html'; a.textContent = 'Go';"
        " document.body.append(a); }"
    )
    t = await browser.observe()
    t0 = time.perf_counter()
    t = await browser.act("CLICK", idx(t, "'Go'"))
    assert "title: Basic" in t
    assert time.perf_counter() - t0 < 1.5


# --- settling after an action ---


async def test_act_returns_after_fetch_renders(browser, origins):
    t = await browser.navigate(f"{origins[0]}/network.html")
    t0 = time.perf_counter()
    t = await browser.act("CLICK", idx(t, "Fetch then render"))
    assert "Loaded result" in t
    assert time.perf_counter() - t0 < 1.5


async def test_long_request_does_not_hold_act(browser, origins):
    t = await browser.navigate(f"{origins[0]}/network.html")
    t0 = time.perf_counter()
    t = await browser.act("CLICK", idx(t, "Start long poll"))
    assert "polling" in t
    assert time.perf_counter() - t0 < 1.3


async def test_navigate_waits_for_first_data_load(browser, origins):
    t = await browser.navigate(f"{origins[0]}/network.html?boot")
    assert "button 'Booted'" in t
