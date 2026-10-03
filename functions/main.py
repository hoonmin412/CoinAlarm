"""Firebase Cloud Functions 진입점.

Cloud Scheduler가 매시 정각(KST)에 이 함수를 호출한다. 코인은 24시간 거래라 시간대 제한이 없다.
"""

from firebase_functions import options, scheduler_fn

import coin_alert


@scheduler_fn.on_schedule(
    schedule="0 * * * *",
    timezone=scheduler_fn.Timezone("Asia/Seoul"),
    secrets=["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
    memory=options.MemoryOption.MB_256,
    timeout_sec=120,
)
def coin_alert_job(event: scheduler_fn.ScheduledEvent) -> None:
    try:
        coin_alert.main()
    except Exception as exc:  # noqa: BLE001
        coin_alert.log.exception("실행 중 오류 발생")
        coin_alert.notify_fatal_error(exc)
        raise
