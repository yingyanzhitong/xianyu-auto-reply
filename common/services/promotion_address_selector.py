from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger


def _normalize_address_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _is_detached_element_error(error: Exception) -> bool:
    """判断 Playwright 元素句柄是否因页面重绘而失效。"""
    message = str(error).lower()
    return "not attached to the dom" in message or "element is not attached" in message


def _is_unavailable_element_error(error: Exception) -> bool:
    """判断动态候选在点击时已失效、隐藏或不可用。"""
    message = str(error).lower()
    return (
        _is_detached_element_error(error)
        or "element is not visible" in message
        or "element is not enabled" in message
    )


async def _click_with_detached_retry(
    target: Any,
    refresh_target: Callable[[], Awaitable[Any]],
    target_name: str,
) -> None:
    """点击动态列表元素；页面重绘导致句柄失效时重新定位一次后重试。"""
    current_target = target
    for attempt in range(2):
        try:
            await current_target.click(timeout=3000)
            return
        except Exception as error:
            if not _is_detached_element_error(error) or attempt == 1:
                raise

            logger.info(f"ℹ️ {target_name}在点击前已刷新，重新定位后重试")
            await asyncio.sleep(0.3)
            current_target = await refresh_target()
            if current_target is None:
                raise Exception(f"{target_name}刷新后未找到") from error


def _get_promotion_address_match_score(
    option_text: str,
    expected_text: str,
    address: str,
) -> tuple[int, int] | None:
    """返佣链路的历史候选匹配策略，保持既有行为不变。"""
    normalized_option = _normalize_address_text(option_text)
    if not normalized_option:
        return None

    normalized_expected = _normalize_address_text(expected_text)
    normalized_address = _normalize_address_text(address)
    if not any(
        target in normalized_option or normalized_option in target
        for target in (normalized_expected, normalized_address)
        if target
    ):
        return None

    match_level = 4
    if normalized_expected:
        if normalized_option == normalized_expected:
            match_level = 0
        elif normalized_expected in normalized_option:
            match_level = 1
    if match_level == 4 and normalized_address:
        if normalized_option == normalized_address:
            match_level = 2
        elif normalized_address in normalized_option:
            match_level = 3
    return (match_level, len(option_text))


_ADDRESS_COMPONENT_PATTERN = re.compile(
    r"[^省市区县旗盟地区]+?(?:特别行政区|自治区|自治州|省|市|区|县|旗|盟|地区)"
)
_ADDRESS_COMPONENT_SUFFIX_PATTERN = re.compile(
    r"(?:特别行政区|自治区|自治州|省|市|区|县|旗|盟|地区)$"
)


def _get_shop_address_match_score(
    option_text: str,
    expected_text: str,
    address: str,
) -> tuple[int, int, int, int] | None:
    """鱼小铺候选匹配策略，兼容省市区顺序变化或省份省略。"""
    normalized_option = _normalize_address_text(option_text)
    if not normalized_option:
        return None

    best_score = None
    for target_index, raw_target in enumerate((expected_text, address)):
        target = _normalize_address_text(raw_target)
        if not target:
            continue
        if normalized_option == target:
            score = (0, target_index, len(normalized_option), 0)
        elif target in normalized_option:
            score = (1, target_index, len(normalized_option), 0)
        elif len(normalized_option) >= 4 and normalized_option in target:
            score = (2, target_index, len(normalized_option), 0)
        else:
            components = [
                _ADDRESS_COMPONENT_SUFFIX_PATTERN.sub("", component)
                for component in _ADDRESS_COMPONENT_PATTERN.findall(target)
            ]
            components = list(dict.fromkeys(component for component in components if len(component) >= 2))
            matched_count = sum(component in normalized_option for component in components)
            if matched_count < (2 if len(components) >= 2 else 1):
                continue
            score = (3, -matched_count, target_index, len(normalized_option))

        if best_score is None or score < best_score:
            best_score = score

    return best_score


def _matches_selected_address_alias(selected_text: str, candidate_text: str) -> bool:
    """确认鱼小铺回填的地点名属于刚刚点击的候选项。"""
    selected = _normalize_address_text(selected_text)
    candidate_label = _normalize_address_text(candidate_text.splitlines()[0] if candidate_text else "")
    if len(selected) < 2 or len(candidate_label) < 2:
        return False
    return selected == candidate_label or selected in candidate_label or candidate_label in selected


async def _click_alternative_amap_options(
    *,
    page: Any,
    option_text: str,
    expected_text: str,
    address: str,
    address_match_score: Callable[[str, str, str], tuple[int, ...] | None],
) -> bool:
    """高德首项为搜索词回显时，依次尝试后续真实 POI 候选。"""
    try:
        candidates = await page.query_selector_all(".amap-sug-result .auto-item")
    except Exception:
        return False

    for candidate in candidates:
        try:
            if not await candidate.is_visible():
                continue
            candidate_text = re.sub(r"\s+", " ", str(await candidate.inner_text() or "")).strip()
            if not candidate_text or _normalize_address_text(candidate_text) == _normalize_address_text(option_text):
                continue
            if address_match_score(candidate_text, expected_text, address) is None:
                continue

            logger.info(f"ℹ️ 高德首项未回填，尝试后续 POI 候选: {candidate_text}")
            await candidate.click(timeout=3000)
            await asyncio.sleep(0.8)
            _, selected_text = await _find_promotion_address_entry(page)
            if address_match_score(selected_text, expected_text, address) is not None:
                logger.info("✅ 鱼小铺地址候选已通过后续 POI 回填")
                return True
            if _matches_selected_address_alias(selected_text, candidate_text):
                logger.info("✅ 鱼小铺地址候选已通过后续 POI 回填")
                return True
        except Exception as exc:
            if _is_unavailable_element_error(exc):
                continue
            logger.info(f"ℹ️ 后续 POI 候选点击未生效: {exc}")

    return False


async def _click_shop_address_option(
    *,
    page: Any,
    option: Any,
    option_text: str,
    expected_text: str,
    address: str,
    address_match_score: Callable[[str, str, str], tuple[int, ...] | None],
    refresh_option: Callable[[], Awaitable[Any]] | None = None,
) -> None:
    """依次点击鱼小铺候选及其可点击父容器，直到地点实际回填。"""
    targets = [option]
    current = option
    for _ in range(6):
        try:
            parent = await current.query_selector("xpath=..")
            if not parent or not await parent.is_visible():
                break
            box = await parent.bounding_box()
            if not box or box.get("height", 0) > 320 or box.get("width", 0) > 1400:
                break
            targets.append(parent)
            current = parent
        except Exception:
            break

    has_detached_target = False
    for target in targets:
        try:
            if not await target.is_enabled():
                continue
        except Exception:
            pass

        try:
            await target.click()
        except Exception as exc:
            if _is_detached_element_error(exc):
                has_detached_target = True
                continue
            if _is_unavailable_element_error(exc):
                continue
            raise

        await asyncio.sleep(0.8)
        _, selected_text = await _find_promotion_address_entry(page)
        if address_match_score(selected_text, expected_text, address) is not None:
            return
        if _matches_selected_address_alias(selected_text, option_text):
            logger.info("✅ 鱼小铺地址候选已通过可点击容器回填")
            return

        if target is option and await _click_alternative_amap_options(
            page=page,
            option_text=option_text,
            expected_text=expected_text,
            address=address,
            address_match_score=address_match_score,
        ):
            return

        try:
            await target.click(force=True)
        except Exception as exc:
            if _is_detached_element_error(exc):
                has_detached_target = True
                continue
            if _is_unavailable_element_error(exc):
                continue
            raise

        await asyncio.sleep(0.8)
        _, selected_text = await _find_promotion_address_entry(page)
        if address_match_score(selected_text, expected_text, address) is not None:
            return
        if _matches_selected_address_alias(selected_text, option_text):
            logger.info("✅ 鱼小铺地址候选已通过强制点击容器回填")
            return

        mouse = getattr(page, "mouse", None)
        box = await target.bounding_box()
        if mouse and box:
            try:
                await mouse.click(
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                )
            except Exception as exc:
                if _is_unavailable_element_error(exc):
                    continue
                raise

            await asyncio.sleep(0.8)
            _, selected_text = await _find_promotion_address_entry(page)
            if address_match_score(selected_text, expected_text, address) is not None:
                logger.info("✅ 鱼小铺地址候选已通过坐标点击容器回填")
                return
            if _matches_selected_address_alias(selected_text, option_text):
                logger.info("✅ 鱼小铺地址候选已通过坐标点击容器回填")
                return

    if has_detached_target and refresh_option:
        refreshed_option = await refresh_option()
        if refreshed_option is not None and refreshed_option is not option:
            logger.info("ℹ️ 鱼小铺地址候选在点击时已刷新，重新定位可用容器")
            return await _click_shop_address_option(
                page=page,
                option=refreshed_option,
                option_text=option_text,
                expected_text=expected_text,
                address=address,
                address_match_score=address_match_score,
            )

    _, selected_text = await _find_promotion_address_entry(page)
    raise Exception(
        "宝贝所在地候选点击未生效，"
        f"目标候选: {option_text or '空'}，当前显示: {selected_text or '空'}"
    )


async def _read_promotion_address_text(container) -> str:
    candidate_selectors = [
        'div[title]',
        'span[title]',
        'div[class*="address"]',
        'span[class*="address"]',
        'div',
        'span',
    ]

    for selector in candidate_selectors:
        try:
            candidates = await container.query_selector_all(selector)
        except Exception:
            continue

        for candidate in candidates:
            try:
                if not await candidate.is_visible():
                    continue
                title_text = str(await candidate.get_attribute("title") or "").strip()
                inner_text = re.sub(r"\s+", " ", str(await candidate.inner_text() or "")).strip()
                text = title_text or inner_text
                normalized_text = _normalize_address_text(text)
                if not normalized_text:
                    continue
                if "宝贝所在地" in text:
                    continue
                if len(normalized_text) > 60:
                    continue
                return text
            except Exception:
                continue

    try:
        container_text = re.sub(r"\s+", " ", str(await container.inner_text() or "")).strip()
    except Exception:
        container_text = ""
    if "宝贝所在地" in container_text:
        container_text = container_text.replace("宝贝所在地", "").strip()
    return container_text


async def _find_promotion_address_entry(page):
    trigger_selectors = [
        'div[class*="addressWrp"]',
        'div[class*="address-wrp"]',
        'div[class*="addressWrap"]',
        'xpath=//*[contains(normalize-space(.), "宝贝所在地")]/following::div[contains(@class, "addressWrp")][1]',
        'xpath=//*[contains(normalize-space(.), "宝贝所在地")]/following::div[contains(@class, "address")][1]',
    ]

    for selector in trigger_selectors:
        try:
            candidates = await page.query_selector_all(selector)
        except Exception:
            continue

        for candidate in candidates:
            try:
                if not await candidate.is_visible():
                    continue
                box = await candidate.bounding_box()
                if not box or box.get("height", 0) < 20 or box.get("width", 0) < 80:
                    continue
                text = await _read_promotion_address_text(candidate)
                has_arrow = False
                try:
                    has_arrow = await candidate.query_selector('[class*="arrow"]') is not None
                except Exception:
                    has_arrow = False
                if not text and not has_arrow:
                    continue
                return candidate, text
            except Exception:
                continue

    return None, ""


async def _find_best_promotion_address_option(
    roots: list[tuple[str, Any]],
    input_box: dict[str, float] | None,
    expected_text: str,
    address: str,
    address_match_score: Callable[[str, str, str], tuple[int, ...] | None],
) -> tuple[Any | None, str]:
    """从当前地址搜索结果中返回得分最高的可点击候选。"""
    option_selectors = [
        '[class*="item"]',
        '[class*="option"]',
        '[role="option"]',
        'li',
        'div',
        'span',
        'button',
        'a',
    ]
    best_option = None
    best_text = ""
    best_score = None

    for root_name, root in roots:
        for selector in option_selectors:
            try:
                options = await root.query_selector_all(selector)
            except Exception:
                continue

            for option in options:
                try:
                    if not await option.is_visible():
                        continue
                    option_text = re.sub(r"\s+", " ", str(await option.inner_text() or "")).strip()
                    normalized_option_text = _normalize_address_text(option_text)
                    if not normalized_option_text:
                        continue
                    if any(text in option_text for text in ["宝贝所在地", "搜索", "清空", "常用地址", "附近地址", "选择精准地址", "帮你推给更多同城买家"]):
                        continue
                    if len(normalized_option_text) < 2 or len(normalized_option_text) > 80:
                        continue
                    match_score = address_match_score(option_text, expected_text, address)
                    if match_score is None:
                        continue
                    box = await option.bounding_box()
                    if not box or box.get("height", 0) < 18 or box.get("width", 0) < 40:
                        continue
                    if input_box:
                        if box.get("y", 0) + box.get("height", 0) <= input_box.get("y", 0):
                            continue
                        if box.get("y", 0) - input_box.get("y", 0) > 700:
                            continue
                        if box.get("x", 0) + box.get("width", 0) < input_box.get("x", 0) - 120:
                            continue
                        if box.get("x", 0) > input_box.get("x", 0) + input_box.get("width", 0) + 360:
                            continue

                    score = (
                        *match_score,
                        box.get("y", 0),
                        box.get("x", 0),
                        0 if root_name == "地址选择层" else 1,
                    )
                    if best_score is None or score < best_score:
                        best_option = option
                        best_text = option_text
                        best_score = score
                except Exception:
                    continue

    return best_option, best_text


async def set_promotion_item_address(
    publisher: Any,
    item_data: dict,
    fallback_set_item_address: Callable[[dict], Awaitable[None]],
    address_match_score: Callable[[str, str, str], tuple[int, ...] | None] = _get_promotion_address_match_score,
    retry_detached_option_click: bool = False,
    allow_selected_address_alias: bool = False,
    retry_unapplied_option_click: bool = False,
) -> None:
    page = publisher.page
    if not page:
        raise Exception("浏览器页面未初始化")

    address = str(item_data.get("address") or "").strip()
    expected_text = str(item_data.get("address_expected_text") or "").strip()
    if not address:
        raise Exception("未获取到可用的宝贝所在地")

    if expected_text:
        logger.info(f"\n[步骤13] 📍 设置宝贝所在地，搜索关键词: {address}，期望文本: {expected_text}")
    else:
        logger.info(f"\n[步骤13] 📍 设置宝贝所在地，搜索关键词: {address}")

    trigger, current_text = await _find_promotion_address_entry(page)
    if not trigger:
        logger.warning("⚠️ 返佣页面未识别到卖家页地址入口，回退通用地址逻辑继续尝试")
        return await fallback_set_item_address(item_data)

    if current_text:
        logger.info(f"当前宝贝所在地: {current_text}")
        if address_match_score(current_text, expected_text, address) is not None:
            logger.info("✅ 当前宝贝所在地已符合要求，跳过设置")
            return

    text_node = None
    try:
        text_node = await trigger.query_selector('[title], div[class*="address"], span[class*="address"]')
    except Exception:
        text_node = None

    click_targets = [trigger]
    if text_node:
        click_targets.insert(0, text_node)

    clicked = False
    for click_target in click_targets:
        try:
            await click_target.click(timeout=3000)
            clicked = True
            break
        except Exception:
            try:
                await click_target.click(timeout=3000, force=True)
                clicked = True
                break
            except Exception:
                continue

    if not clicked:
        raise Exception("未找到宝贝所在地设置入口")

    await asyncio.sleep(1.5)

    panel = None
    panel_selectors = [
        '.ant-modal-content',
        '.ant-modal-wrap',
        '.ant-drawer-content',
        '.ant-drawer-body',
        '[role="dialog"]',
        '[class*="drawer"]',
        '[class*="popover"]',
        '[class*="dropdown"]',
        '[class*="addressPanel"]',
    ]
    for selector in panel_selectors:
        try:
            panels = await page.query_selector_all(selector)
        except Exception:
            continue

        for current_panel in panels:
            try:
                if not await current_panel.is_visible():
                    continue
                panel_text = re.sub(r"\s+", " ", str(await current_panel.inner_text() or "")).strip()
                if any(text in panel_text for text in ["宝贝所在地", "常用地址", "附近地址", "精准地址", "小区", "写字楼", "学校", "搜索"]):
                    panel = current_panel
                    logger.info(f"✅ 已识别返佣地址选择层: {selector}")
                    break
            except Exception:
                continue

        if panel:
            break

    roots: list[tuple[str, Any]] = []
    if panel:
        roots.append(("地址选择层", panel))
    else:
        logger.info("ℹ️ 未识别到独立地址弹层，改为在当前页面继续查找地址搜索框")
    roots.append(("页面", page))

    input_selectors = [
        'input[placeholder*="请输入"]',
        'input[placeholder*="地址"]',
        'input[placeholder*="位置"]',
        'input[placeholder*="小区"]',
        'input[placeholder*="学校"]',
        'input[placeholder*="写字楼"]',
        'input[placeholder*="搜索"]',
        'input[aria-label*="地址"]',
        'input[aria-label*="搜索"]',
        'input',
    ]

    search_input = None
    input_box = None
    for root_name, root in roots:
        for selector in input_selectors:
            try:
                inputs = await root.query_selector_all(selector)
            except Exception:
                continue

            for current_input in inputs:
                try:
                    if not await current_input.is_visible():
                        continue
                    placeholder = str(await current_input.get_attribute("placeholder") or "")
                    aria_label = str(await current_input.get_attribute("aria-label") or "")
                    normalized_marker = _normalize_address_text(f"{placeholder}{aria_label}")
                    if selector == 'input' and panel is None and not any(keyword in normalized_marker for keyword in ["请输入", "地址", "位置", "搜索", "小区", "学校", "写字楼"]):
                        continue
                    box = await current_input.bounding_box()
                    if not box or box.get("height", 0) < 20 or box.get("width", 0) < 80:
                        continue
                    search_input = current_input
                    input_box = box
                    logger.info(f"✅ 在{root_name}中找到宝贝所在地搜索框: {selector}")
                    break
                except Exception:
                    continue

            if search_input:
                break

        if search_input:
            break

    if not search_input:
        _, refreshed_text = await _find_promotion_address_entry(page)
        if refreshed_text:
            if address_match_score(refreshed_text, expected_text, address) is not None:
                logger.info("✅ 点击地址入口后已自动匹配到目标地址")
                return
        raise Exception("未找到宝贝所在地搜索框")

    await search_input.click()
    await asyncio.sleep(0.3)
    try:
        await search_input.fill("")
    except Exception:
        try:
            await search_input.press("Control+A")
            await search_input.press("Backspace")
        except Exception:
            pass
    await asyncio.sleep(0.4)
    await search_input.type(address, delay=150)
    await asyncio.sleep(2.5)

    best_option, best_text = await _find_best_promotion_address_option(
        roots=roots,
        input_box=input_box,
        expected_text=expected_text,
        address=address,
        address_match_score=address_match_score,
    )

    if not best_option:
        raise Exception(f"未找到“{address}”对应的宝贝所在地候选")

    logger.info(f"🎯 选择返佣宝贝所在地候选: {best_text}")

    async def refresh_best_option():
        refreshed_option, _ = await _find_best_promotion_address_option(
            roots=roots,
            input_box=input_box,
            expected_text=expected_text,
            address=address,
            address_match_score=address_match_score,
        )
        return refreshed_option

    if retry_unapplied_option_click:
        # 鱼小铺候选的文本子节点可能不可用，首次点击也必须从可用容器开始。
        await _click_shop_address_option(
            page=page,
            option=best_option,
            option_text=best_text,
            expected_text=expected_text,
            address=address,
            address_match_score=address_match_score,
            refresh_option=refresh_best_option if retry_detached_option_click else None,
        )
    elif retry_detached_option_click:
        await _click_with_detached_retry(
            target=best_option,
            refresh_target=refresh_best_option,
            target_name=f"宝贝所在地候选“{best_text}”",
        )
    else:
        await best_option.click()
    await asyncio.sleep(1)

    confirm_selectors = [
        '.ant-modal-footer button.ant-btn-primary',
        '.ant-modal-footer button',
        'button:has-text("确定")',
        'button:has-text("确认")',
        'button:has-text("完成")',
        '[role="button"]:has-text("确定")',
        '[role="button"]:has-text("确认")',
    ]
    for root_name, root in roots:
        confirmed = False
        for selector in confirm_selectors:
            try:
                confirm_button = await root.query_selector(selector)
                if confirm_button and await confirm_button.is_visible() and await confirm_button.is_enabled():
                    await confirm_button.click()
                    await asyncio.sleep(1)
                    logger.info(f"✅ 已在{root_name}中确认宝贝所在地")
                    confirmed = True
                    break
            except Exception:
                continue
        if confirmed:
            break

    await asyncio.sleep(1)
    _, selected_text = await _find_promotion_address_entry(page)
    if selected_text:
        logger.info(f"当前已选择宝贝所在地: {selected_text}")
        if address_match_score(selected_text, expected_text, address) is not None:
            logger.info("✅ 宝贝所在地设置完成")
            return
        if allow_selected_address_alias and _matches_selected_address_alias(selected_text, best_text):
            logger.info("✅ 鱼小铺已选中匹配候选，保留平台展示的地址别名")
            return

    raise Exception(
        "宝贝所在地设置后校验失败，"
        f"目标候选: {best_text or '空'}，当前显示: {selected_text or '空'}"
    )
