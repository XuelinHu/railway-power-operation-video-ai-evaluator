"""视频提交：流式上传、校验、名册绑定、按权限取回。

## 为什么必须流式写盘

V1 是 `content = await file.read()` —— 整个视频一次性进内存。
100 个学生在实训室同时上传 500MB 的视频，就是 50GB 的峰值内存需求，
进程会被 OOM killer 直接杀掉，而且**杀掉的是 API 进程**——
所有人的页面一起变成 502，包括那些只是来查成绩的。

改成边读边写边算哈希：内存占用恒定在 1MB 的分块大小，
代价只是多一次磁盘写（本来就要写）。

## 为什么学生不能自己填姓名学号

V1 把 `student_name` / `student_no` 当普通表单字段收。于是"张三"可以提交
写着"李四"的作业，而系统没有任何办法发现。成绩对账更是无从谈起。

现在学生只能提交**名册上属于自己那一条**，姓名学号一律从名册取，
表单里传什么都不作数。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Annotated, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlmodel import Session, select

from ..auth import current_user
from ..config import settings
from ..database import UPLOAD_DIR, get_session
from ..models import (
    AnalysisJob,
    AnalysisStatus,
    RosterEntry,
    TrainingTask,
    User,
    UserRole,
    VideoSubmission,
)
from ..services import audit
from ..services.video import UploadRejected, probe_or_reject

router = APIRouter(prefix="/api/submissions", tags=["submissions"])

# 分块大小。1MB 是"内存占用"与"系统调用次数"之间的常用折中：
# 2GB 的文件是 2048 次 write，对本地磁盘完全无感。
_CHUNK_BYTES = 1024 * 1024

# 允许的容器。**这不是安全防线**（判据是 ffprobe 能否读出时长），
# 只是为了让明显的误传在读取整个文件之前就被挡掉。
_ALLOWED_SUFFIXES = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".3gp", ".flv", ".wmv"}

_BLOCKING_JOB_STATES = {AnalysisStatus.pending, AnalysisStatus.running, AnalysisStatus.completed}


def _play_path(stored_filename: str) -> Path:
    """转码产物的路径。转码在 worker 里做，上传接口不等它。"""
    return UPLOAD_DIR / f"{Path(stored_filename).stem}.play.mp4"


def _view(row: VideoSubmission, job: Optional[AnalysisJob]) -> dict:
    return {
        "id": row.id,
        "task_id": row.task_id,
        "student_name": row.student_name,
        "student_no": row.student_no,
        "original_filename": row.original_filename,
        "size_bytes": row.size_bytes,
        "duration_sec": row.duration_sec,
        "transcode_state": row.transcode_state,
        "uploaded_at": row.uploaded_at,
        "job": None
        if job is None
        else {
            "id": job.id,
            "status": job.status,
            "score": job.score,
            "progress": job.progress,
            "stage": job.stage,
            # 只给教师可读的那份。error_message 是内部排障用的，
            # 里面可能有绝对路径，投给前端等于泄露服务器目录结构。
            "error": job.error_code,
        },
    }


def _visible_submissions(session: Session, user: User) -> list[VideoSubmission]:
    """按角色限定可见范围。

    学生只看得到自己的——这不是"防君子"的界面约束，而是数据边界：
    V1 里任何登录用户都能遍历 `/api/submissions` 拿到全班的姓名学号。
    """
    query = select(VideoSubmission).order_by(VideoSubmission.uploaded_at.desc())
    if user.role == UserRole.student:
        query = query.where(VideoSubmission.student_id == user.id)
    return list(session.exec(query).all())


@router.get("")
def list_submissions(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
    task_id: int | None = None,
):
    rows = _visible_submissions(session, user)
    if task_id:
        rows = [row for row in rows if row.task_id == task_id]

    jobs = {
        job.submission_id: job
        for job in session.exec(
            select(AnalysisJob).where(AnalysisJob.submission_id.in_([row.id for row in rows] or [0]))
        ).all()
    }
    return [_view(row, jobs.get(row.id)) for row in rows]


@router.post("")
async def upload_submission(
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
    file: Annotated[UploadFile, File()],
    task_id: Annotated[int, Form()],
    student_no: Annotated[str, Form()] = "",
):
    task = session.get(TrainingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="作业任务不存在。")

    entry = _resolve_roster_entry(session, user, task, student_no)
    _reject_duplicate(session, entry)

    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件格式（{suffix or '无扩展名'}）。请上传手机或摄像机导出的视频文件。",
        )

    stored_filename = f"{uuid4().hex}{suffix}"
    target = UPLOAD_DIR / stored_filename
    size_bytes, digest = await _write_stream(file, target)

    try:
        # 时长校验放在写盘之后：ffprobe 必须先有完整文件。
        # 这一步同时挡住"把 .txt 改名成 .mp4"——判据是能不能解码，
        # 而不是扩展名。
        duration = probe_or_reject(target)
    except UploadRejected as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        target.unlink(missing_ok=True)
        raise HTTPException(
            status_code=400, detail="视频校验失败，请确认文件能正常播放后重新上传。"
        ) from exc

    # 同一份文件重复上传（学生刷新页面重传）直接复用已有记录会带来
    # 一堆边界情况，这里只做提示性的指纹记录，不做去重。
    duplicate = session.exec(
        select(VideoSubmission).where(
            VideoSubmission.task_id == task_id, VideoSubmission.sha256 == digest
        )
    ).first()

    submission = VideoSubmission(
        task_id=task_id,
        student_name=entry.student_name,
        student_no=entry.student_no,
        original_filename=file.filename or stored_filename,
        stored_filename=stored_filename,
        content_type=file.content_type or "",
        size_bytes=size_bytes,
        student_id=entry.user_id or user.id,
        roster_entry_id=entry.id,
        duration_sec=duration,
        sha256=digest,
        transcode_state="pending",
    )
    session.add(submission)
    session.commit()
    session.refresh(submission)

    job = AnalysisJob(submission_id=submission.id, progress=0, stage="排队中，等待分析")
    session.add(job)
    session.commit()
    session.refresh(job)

    audit.record(
        session,
        action="upload_submission",
        user=user,
        target_type="submission",
        target_id=submission.id,
        detail=f"{entry.student_no} 上传 {submission.original_filename}"
        f"（{size_bytes / 1024 / 1024:.1f}MB，{duration:.0f}秒）",
    )

    result = _view(submission, job)
    if duplicate is not None:
        result["notice"] = "这个视频文件此前已提交过，请注意是否重复提交。"
    return result


# ---------------------------------------------------------------------------
# 上传辅助
# ---------------------------------------------------------------------------


async def _write_stream(file: UploadFile, target: Path) -> tuple[int, str]:
    """边读边写边算哈希，内存占用恒定。

    大小上限在**写入过程中**判：等写完再判，2GB 已经落盘了，
    磁盘会被少量恶意（或误操作）请求迅速填满，而磁盘满会让整个系统停摆。
    """
    digest = hashlib.sha256()
    size = 0

    with target.open("wb") as handle:
        while chunk := await file.read(_CHUNK_BYTES):
            size += len(chunk)
            if size > settings.max_upload_bytes:
                handle.close()
                target.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"视频文件超过 {settings.max_upload_bytes / 1024 / 1024 / 1024:.1f}GB 上限，"
                    "请压缩或截取后再上传。",
                )
            digest.update(chunk)
            handle.write(chunk)

    if size == 0:
        target.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="上传的文件是空的，请重新选择视频文件。")

    return size, digest.hexdigest()


def _resolve_roster_entry(
    session: Session, user: User, task: TrainingTask, student_no: str
) -> RosterEntry:
    """确定这次提交归属名册上的哪一条。

    学生只能提交自己那一条；教师/管理员可以代传（用于演示和补交），
    此时按传入的学号找名册条目。
    """
    if user.role == UserRole.student:
        entry = session.exec(
            select(RosterEntry).where(
                RosterEntry.task_id == task.id, RosterEntry.user_id == user.id
            )
        ).first()

        if entry is None and user.student_no:
            # 名单先于账号建立是常态（教师先导入名册，学生后来才登录），
            # 所以这里做一次自动绑定，而不是让学生卡在"你不在名单里"。
            entry = session.exec(
                select(RosterEntry).where(
                    RosterEntry.task_id == task.id, RosterEntry.student_no == user.student_no
                )
            ).first()
            if entry is not None and entry.user_id is None:
                entry.user_id = user.id
                session.add(entry)
                session.commit()

        if entry is None:
            raise HTTPException(
                status_code=403,
                detail=f"你不在「{task.title}」的名单中，无法提交。请联系任课教师确认名单。",
            )
        return entry

    if not student_no.strip():
        raise HTTPException(status_code=400, detail="请指定学生学号。")
    entry = session.exec(
        select(RosterEntry).where(
            RosterEntry.task_id == task.id, RosterEntry.student_no == student_no.strip()
        )
    ).first()
    if entry is None:
        raise HTTPException(
            status_code=404,
            detail=f"名单中没有学号 {student_no}。请先在名册中导入该学生。",
        )
    return entry


def _reject_duplicate(session: Session, entry: RosterEntry) -> None:
    """同一名册条目只允许有一份**有效**提交。

    允许在上一份分析失败后重传：那是系统的错，不该让学生承担。
    但正在分析中或已完成时重复提交一律拒绝——否则学生连点三次
    "提交"，队列里就多三个任务，白花三份钱。
    """
    existing = session.exec(
        select(VideoSubmission).where(VideoSubmission.roster_entry_id == entry.id)
    ).all()
    if not existing:
        return

    jobs = session.exec(
        select(AnalysisJob).where(
            AnalysisJob.submission_id.in_([row.id for row in existing])
        )
    ).all()
    blocking = [job for job in jobs if job.status in _BLOCKING_JOB_STATES]
    if blocking:
        raise HTTPException(
            status_code=409,
            detail="你已经提交过本次作业了。如需重新提交，请先联系任课教师。",
        )


# ---------------------------------------------------------------------------
# 取回
# ---------------------------------------------------------------------------


@router.get("/{submission_id}/video")
def get_video(
    submission_id: int,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    submission = session.get(VideoSubmission, submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在。")

    if user.role == UserRole.student and submission.student_id != user.id:
        # 返回 404 而不是 403：403 会告诉学生"这个 id 存在但不属于你"，
        # 遍历 id 就能统计出全班提交了多少份。
        raise HTTPException(status_code=404, detail="提交记录不存在。")

    # 有转码产物就优先给转码产物：手机拍的 HEVC 在 Windows 版 Chrome 里
    # 放不出画面，教师复核时看到黑屏会以为视频坏了。
    play = _play_path(submission.stored_filename)
    path = play if play.exists() else UPLOAD_DIR / submission.stored_filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="视频文件不存在，可能已被清理。")

    return FileResponse(
        path,
        media_type="video/mp4" if path is play else (submission.content_type or "video/mp4"),
        filename=submission.original_filename,
        # 默认是 attachment，浏览器会变成下载而不是播放。
        # 教师复核要在页面里边看边核对证据帧，必须 inline。
        content_disposition_type="inline",
        # FileResponse 已支持 Range 请求，播放器才能拖动进度条
        # （"点击证据帧跳到视频对应时间点"整个功能都依赖它）。
    )


@router.get("/{submission_id}/frames")
def list_frames(
    submission_id: int,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    """证据帧清单：帧号、对应视频时间、图片地址。

    "点击缩略图跳到视频第几秒"是教师核对 AI 判定是否靠谱的唯一手段——
    没有它，教师只能选择全信或全不信，复核就退化成了橡皮图章。
    """
    from ..database import FRAMES_DIR

    submission = session.get(VideoSubmission, submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在。")
    if user.role == UserRole.student and submission.student_id != user.id:
        raise HTTPException(status_code=404, detail="提交记录不存在。")

    job = session.exec(
        select(AnalysisJob).where(AnalysisJob.submission_id == submission_id)
    ).first()
    if not job:
        return {"frames": [], "frame_times": []}

    run_no = max(1, job.attempts)
    directory = FRAMES_DIR / str(job.id) / str(run_no)

    import json

    meta_path = directory / "meta.json"
    frame_times: list[float] = []
    if meta_path.exists():
        try:
            frame_times = json.loads(meta_path.read_text(encoding="utf-8")).get("frame_times", [])
        except (ValueError, OSError):
            frame_times = []

    frames = sorted(directory.glob("f*.jpg"))
    return {
        "job_id": job.id,
        "frames": [
            {
                "index": index,
                "timestamp_sec": frame_times[index - 1] if index - 1 < len(frame_times) else None,
                "url": f"/api/submissions/{submission_id}/frames/{index}",
            }
            for index in range(1, len(frames) + 1)
        ],
    }


@router.get("/{submission_id}/frames/{index}")
def get_frame(
    submission_id: int,
    index: int,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[Session, Depends(get_session)],
):
    from ..database import FRAMES_DIR

    submission = session.get(VideoSubmission, submission_id)
    if not submission:
        raise HTTPException(status_code=404, detail="提交记录不存在。")
    if user.role == UserRole.student and submission.student_id != user.id:
        raise HTTPException(status_code=404, detail="提交记录不存在。")

    job = session.exec(
        select(AnalysisJob).where(AnalysisJob.submission_id == submission_id)
    ).first()
    if not job:
        raise HTTPException(status_code=404, detail="尚未分析。")

    # index 来自 URL，必须限定成纯数字文件名再去拼路径，
    # 否则 `../../etc/passwd` 这类输入会变成目录穿越。
    if index < 1 or index > 999:
        raise HTTPException(status_code=404, detail="帧不存在。")

    path = FRAMES_DIR / str(job.id) / str(max(1, job.attempts)) / f"f{index:03d}.jpg"
    if not path.exists():
        raise HTTPException(status_code=404, detail="帧不存在。")
    return FileResponse(path, media_type="image/jpeg", content_disposition_type="inline")
