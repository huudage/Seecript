# 功能盘规格（2026-09-24）

开发以本文为准。产品理由见 `docs/PRD-v2.md` F6。

## 空白画布右键

只出现四项，不出现转场、补拍、配口播。

1. 自然语言改片：点击，打开现有命令条。
2. 生字卡：悬停。字段只有关联结构段、时长、主文案。关联段可选；选中后用该段 `theme` / `content_description` / `duration_seconds` 预填，用户可改。提交 `POST /api/plan/{id}/sections/append`，`source=text_card`。
3. 生图再渲染：悬停。字段只有关联结构段、提示词、时长、渲染方式。点生成后先在画布末尾落下视频块，块内显示「生图中」；接口返回后再换成成图。渲染方式二选一：`hold` 静图停留，`push` 缓慢推近。`source=aigc_image`，`render_motion` 写入 `animation_spec`。

右键唤盘走捕获阶段，且右键不再平移画布。点在连线上时仍唤出空白画布的功能盘，连线自己没有菜单。
4. 添加用户素材：悬停列出素材库，点一条即 append，`source=user_material`。

关联结构段不替换原段，新块追加在链序末尾。

## 视频块右键

只出现字幕、标题条、贴纸、封面。不出现转场。

转场在两块之间的连线上：只响应左键，打开 6 风格选择器。连线没有右键菜单。

- 字幕：`PATCH /scene/{id}` 写 narration，并打开 `subtitle_enabled`。
- 其余三项：`POST /packaging/items/place`，`item_id=blk-{kind}-{scene_id}`，时间窗等于该块起止。再次保存替换同一 id。

## 视频块左键

弹窗，不调用 `scrollIntoView`。

- 字卡：不放播放器。可改主文案、副文案、时长。`swap-source` `source=text_card`，`duration_seconds` 改变时重铺时间轴。
- 用户素材：`<video>` 播放，入点/出点滑杆，确认走 `swap-source` 的 `material_in_point` / `material_out_point`。
- 生图块：只展示图片，不提供落盘裁剪。

## 验收

- 右键空白处能看到上述四项，悬停表单不因鼠标移到表单上而消失。
- 右键视频块只有四项包装，没有转场。
- 左键视频块后，画布上的其他块仍在。
- `tests/test_plan_scene_router.py::test_swap_text_card_duration_follows_request` 通过。
