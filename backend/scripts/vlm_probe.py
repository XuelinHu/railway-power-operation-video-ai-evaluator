#!/usr/bin/env python3
"""Phase 1 可行性闸门：拿真实视频测「通义千问-VL 到底能不能判准实训步骤」。

**为什么这个脚本排在鉴权和 worker 前面**：鉴权、worker、前端是**已知工作量**，
做多久心里有数；VLM 判得准不准是**未知工作量**。按常规先花一周做鉴权，
第二周才发现模型看不清绝缘手套，就一无所有。这半天省不得。

用法：

    # 单个视频
    python -m scripts.vlm_probe 视频.mp4

    # 批量（Phase 1 闸门要求 5 个真实视频）
    python -m scripts.vlm_probe ~/videos/*.mp4 --out probe_result.json

    # 只验抽帧、不花钱（没有 API key 时也能跑）
    python -m scripts.vlm_probe 视频.mp4 --dry-run

**验收标准**（结果里会打印）：
  1. 5 个视频里 ≥4 个、每个 ≥7/9 步与人工判断一致
  2. not_visible 用得合理——既不是全 completed，也不是全 not_visible
  3. 单视频成本 < ¥0.1

不过就触发 Plan B：降级为「AI 只产证据时间轴，由教师在时间轴上打钩」。
那个版本仍有真实价值（老师不必完整看 100 遍视频找时间点），且风险归零。

脚本**不进 API、不入库**，产物是一份可交给老师逐条核对的 JSON。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

# 允许 `python scripts/vlm_probe.py` 与 `python -m scripts.vlm_probe` 两种跑法
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai.ffmpeg import (  # noqa: E402
    FFmpegUnavailable,
    VideoUnreadable,
    require_ffmpeg,
)
from app.services.ai.labels import REVIEW_CONFIDENCE_THRESHOLD  # noqa: E402
from app.services.ai.vlm_analyzer import AnalysisFailed, AnalysisRun, analyze  # noqa: E402
from app.services.ai.vlm_client import VLMClient, VLMError  # noqa: E402

COST_CEILING_CNY = 0.1
ACCURACY_FLOOR = 7 / 9


def _load_dotenv() -> None:
    """从 backend/.env 读 key。没有 python-dotenv 就手工解析，不额外加依赖。"""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path)
        return
    except ImportError:
        pass
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _run_to_dict(run: AnalysisRun, video: Path) -> dict:
    verdicts = Counter(step.verdict for step in run.outcome.steps)
    return {
        "video": video.name,
        "duration_sec": round(run.duration_sec, 1),
        "frames": len(run.frames),
        "elapsed_sec": round(run.elapsed_sec, 1),
        "video_quality": run.outcome.video_quality,
        "scene_summary": run.outcome.scene_summary,
        "overall_note": run.outcome.overall_note,
        "needs_review": run.outcome.needs_review,
        "review_reasons": run.outcome.review_reasons,
        "warnings": run.outcome.warnings,
        "verdict_counts": dict(verdicts),
        "usage": {
            "calls": len(run.calls),
            "prompt_tokens": run.prompt_tokens,
            "completion_tokens": run.completion_tokens,
            "estimated_cost_cny": round(run.estimated_cost_cny, 4),
            "request_ids": run.request_ids,
        },
        "frames_timeline": [
            {"index": i, "sec": round(f.timestamp_sec, 1), "reason": f.reason}
            for i, f in enumerate(run.frames, start=1)
        ],
        # 判定阶段看到的观察表原样存下来：人工核对时先看它，
        # 能立刻分辨"是模型看错了"还是"模型看对了但判错了"——这两者的修法完全不同。
        "observation_table": run.observation_table,
        "steps": [
            {
                "step_code": step.step_code,
                "step_name": step.step_name,
                "verdict": step.verdict,
                "confidence": round(step.confidence, 2),
                "evidence_frames": step.evidence_frames,
                "evidence_times": [
                    round(run.frames[i - 1].timestamp_sec, 1)
                    for i in step.evidence_frames
                    if 1 <= i <= len(run.frames)
                ],
                "reason": step.reason,
                "validation_notes": step.validation_notes,
                "needs_review": step.needs_review,
            }
            for step in run.outcome.steps
        ],
    }


def _print_human(report: dict) -> None:
    """把结果打成老师也能看的形状——Phase 1 的核心动作就是人工逐条核对。"""
    print(f"\n{'=' * 72}")
    print(f"视频：{report['video']}")
    print(f"时长 {report['duration_sec']}s ｜ 抽帧 {report['frames']} 张 ｜ "
          f"耗时 {report['elapsed_sec']}s ｜ 画质 {report['video_quality']}")
    print(f"场景概述：{report['scene_summary']}")
    if report["overall_note"]:
        print(f"模型备注：{report['overall_note']}")
    print("-" * 72)
    print(f"{'步骤':<18}{'判定':<16}{'置信':<7}证据帧（对应秒）")
    print("-" * 72)
    for step in report["steps"]:
        evidence = "、".join(
            f"#{i}({sec}s)" for i, sec in zip(step["evidence_frames"], step["evidence_times"])
        ) or "—"
        flag = " ⚠️" if step["needs_review"] else ""
        print(f"{step['step_name']:<18}{step['verdict']:<16}{step['confidence']:<7}{evidence}{flag}")
        if step["reason"]:
            print(f"{'':<18}└ {step['reason']}")
        for note in step["validation_notes"]:
            print(f"{'':<18}└ [校验] {note}")

    counts = report["verdict_counts"]
    print("-" * 72)
    print(f"判定分布：{counts}")
    for warning in report["warnings"]:
        print(f"⚠️  {warning}")
    if report["needs_review"]:
        print(f"需人工复核：{'；'.join(report['review_reasons'])}")

    usage = report["usage"]
    print(f"用量：{usage['calls']} 次调用 ｜ 输入 {usage['prompt_tokens']} tok ｜ "
          f"输出 {usage['completion_tokens']} tok ｜ 估算 ¥{usage['estimated_cost_cny']}")
    if usage["estimated_cost_cny"] > COST_CEILING_CNY:
        print(f"❌ 成本超过 ¥{COST_CEILING_CNY}/视频 的验收线")


def _print_summary(reports: list[dict]) -> None:
    print(f"\n{'=' * 72}")
    print(f"汇总：{len(reports)} 个视频")
    print("-" * 72)
    total_cost = sum(r["usage"]["estimated_cost_cny"] for r in reports)
    all_visible = 0
    all_completed = 0
    for report in reports:
        counts = report["verdict_counts"]
        visible = counts.get("not_visible", 0)
        if visible == 0:
            all_visible += 1
        if counts.get("completed", 0) == len(report["steps"]):
            all_completed += 1
        print(f"  {report['video']:<32} 帧{report['frames']:>3}  "
              f"{report['elapsed_sec']:>5.1f}s  ¥{report['usage']['estimated_cost_cny']:.4f}  "
              f"{report['verdict_counts']}")

    print("-" * 72)
    print(f"总成本 ¥{total_cost:.4f}，均值 ¥{total_cost / len(reports):.4f}/视频 "
          f"（验收线 < ¥{COST_CEILING_CNY}）")
    if all_completed:
        print(f"❌ 有 {all_completed} 个视频被判成「全部 completed」——"
              "高度可疑，优先怀疑一致性偏置没有被压住，请人工核对。")
    if all_visible:
        print(f"⚠️  有 {all_visible} 个视频「一步都没判 completed」——"
              "可能是机位确实差，也可能是模型过于保守，两种情况要看观察表分辨。")
    print("\n注意：一致率（≥7/9 步与人工一致）无法由脚本自动判定，"
          "需要你与老师对着上面每个视频的判定逐条核对。")
    print("核对时先看 JSON 里的 observation_table：若观察表本身就错了，"
          "要调的是 Pass 1 的 prompt 或帧数与分辨率；若观察表对而判定错，"
          "要调的是 Pass 2 的判定规则。")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="VLM 可行性闸门：验证通义千问-VL 能否判准实训步骤"
    )
    parser.add_argument("videos", nargs="+", type=Path, help="待测视频（Phase 1 建议 5 个）")
    parser.add_argument("--out", type=Path, help="结果 JSON 的输出路径")
    parser.add_argument("--frames", type=int, default=16, help="抽帧预算（默认 16，上限硬约束）")
    parser.add_argument("--windows", type=int, default=3, help="Pass 1 的时间窗数量")
    parser.add_argument("--model", default=os.getenv("VLM_MODEL", ""), help="模型名")
    parser.add_argument("--keep-frames", action="store_true", help="保留抽帧图片供人工比对")
    parser.add_argument("--dry-run", action="store_true",
                        help="只跑抽帧，不调 VLM（无 key 时可用，验证 ffmpeg 链路）")
    parser.add_argument("--json-mode", action="store_true",
                        help="改用 response_format=json_object，绕开强制 tool_choice 的兼容问题")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        ffmpeg, ffprobe = require_ffmpeg()
        print(f"ffmpeg: {ffmpeg}\nffprobe: {ffprobe}")
    except FFmpegUnavailable as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2

    missing = [str(v) for v in args.videos if not v.exists()]
    if missing:
        print(f"❌ 找不到视频文件：{', '.join(missing)}", file=sys.stderr)
        return 2

    if args.dry_run:
        return _dry_run(args.videos, args.frames, args.keep_frames)

    _load_dotenv()
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        print(
            "❌ 未配置 DASHSCOPE_API_KEY。\n"
            "   在 backend/.env 中写入：DASHSCOPE_API_KEY=sk-xxxx\n"
            "   只想先验证抽帧链路可以加 --dry-run（不花钱、不需要 key）。",
            file=sys.stderr,
        )
        return 2

    work_root = Path(os.getenv("PROBE_WORK_DIR", "/tmp/vlm_probe"))
    reports: list[dict] = []
    failures = 0

    model = args.model or None
    client_kwargs = {"model": model} if model else {}
    with VLMClient(api_key, **client_kwargs) as client:
        print(f"模型：{client.model}\n")
        for video in args.videos:
            work_dir = work_root / video.stem
            try:
                run = analyze(
                    client,
                    video,
                    work_dir,
                    frame_budget=args.frames,
                    window_count=args.windows,
                    review_confidence=REVIEW_CONFIDENCE_THRESHOLD,
                    json_object=args.json_mode,
                )
            except (VideoUnreadable, AnalysisFailed) as exc:
                # 视频本身的问题：不重试，直接报给老师
                print(f"\n❌ {video.name}：{exc}", file=sys.stderr)
                failures += 1
                continue
            except VLMError as exc:
                request_id = f"（request_id={exc.request_id}）" if exc.request_id else ""
                print(f"\n❌ {video.name}：{exc}{request_id}", file=sys.stderr)
                failures += 1
                continue

            report = _run_to_dict(run, video)
            reports.append(report)
            _print_human(report)
            if not args.keep_frames:
                for frame in run.frames:
                    frame.path.unlink(missing_ok=True)

    if not reports:
        print("\n没有任何视频分析成功。", file=sys.stderr)
        return 1

    _print_summary(reports)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入 {args.out}")

    return 1 if failures else 0


def _dry_run(videos: list[Path], budget: int, keep: bool) -> int:
    """只验抽帧。没有 API key 时也能确认 ffmpeg 链路是通的。"""
    from app.services.ai.ffmpeg import extract_frames

    print("\n== 干跑模式：只抽帧，不调用 VLM ==\n")
    for video in videos:
        target = Path("/tmp/vlm_probe") / f"{video.stem}_dry"
        try:
            frames = extract_frames(video, target, budget=budget)
        except (VideoUnreadable, FFmpegUnavailable) as exc:
            print(f"❌ {video.name}：{exc}")
            return 1
        span = frames[-1].timestamp_sec if frames else 0
        reasons = Counter(frame.reason for frame in frames)
        size_kb = sum(f.path.stat().st_size for f in frames) / 1024
        print(f"✓ {video.name}：{len(frames)} 帧，末帧 {span:.1f}s，"
              f"{dict(reasons)}，合计 {size_kb:.0f}KB")
        print(f"  产物：{target}")
        if not keep:
            for frame in frames:
                frame.path.unlink(missing_ok=True)
    print("\n抽帧链路正常。配置 DASHSCOPE_API_KEY 后去掉 --dry-run 即可跑真实判定。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
