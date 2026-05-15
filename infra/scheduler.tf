# Keepalive ping every 5 minutes. The handler short-circuits on
# {"warm": true} with HTTP 200 in ~50 ms, keeping one execution environment
# hot. On the first tick of each cycle the handler also self-invokes
# (warm_pool_size - 1) parallel copies to fan-out the warmth across multiple
# environments — see the lambda_self_invoke policy in iam.tf.
#
# Free-tier math: 1 invocation every 5 min × 24h × 30d ≈ 8.6k/month.
# EventBridge Scheduler free tier covers 1M invocations/month indefinitely.
resource "aws_scheduler_schedule" "keepalive" {
  name       = "${var.project_name}-keepalive"
  group_name = "default"
  state      = "ENABLED"

  schedule_expression = "rate(5 minutes)"

  # OFF = strict on-the-tick scheduling. Required block even when not used.
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.app.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ warm = true })

    retry_policy {
      maximum_event_age_in_seconds = 60
      maximum_retry_attempts       = 0
    }
  }
}
