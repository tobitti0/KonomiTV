from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" ADD COLUMN "cm_analysis_error" JSON;
        ALTER TABLE "recorded_videos" ADD COLUMN "cm_analysis_started_at" TIMESTAMP;
        ALTER TABLE "recorded_videos" ADD COLUMN "cm_analysis_completed_at" TIMESTAMP;
        ALTER TABLE "recorded_videos" ADD COLUMN "cm_analysis_elapsed_time" REAL;
        CREATE TABLE IF NOT EXISTS "cm_analysis_runs" (
            "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            "recorded_video_id" INT NOT NULL REFERENCES "recorded_videos" ("id") ON DELETE CASCADE,
            "status" VARCHAR(255) NOT NULL,
            "trigger" VARCHAR(255) NOT NULL,
            "cm_sections" JSON,
            "stage_results" JSON NOT NULL DEFAULT '[]',
            "error" JSON,
            "started_at" TIMESTAMP NOT NULL,
            "completed_at" TIMESTAMP,
            "elapsed_time" REAL,
            "created_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            "updated_at" TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS "idx_cm_analysis_runs_recorded_video_created_at"
            ON "cm_analysis_runs" ("recorded_video_id", "created_at");
        CREATE INDEX IF NOT EXISTS "idx_cm_analysis_runs_status" ON "cm_analysis_runs" ("status");
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP INDEX IF EXISTS "idx_cm_analysis_runs_status";
        DROP INDEX IF EXISTS "idx_cm_analysis_runs_recorded_video_created_at";
        DROP TABLE IF EXISTS "cm_analysis_runs";
        ALTER TABLE "recorded_videos" DROP COLUMN "cm_analysis_elapsed_time";
        ALTER TABLE "recorded_videos" DROP COLUMN "cm_analysis_completed_at";
        ALTER TABLE "recorded_videos" DROP COLUMN "cm_analysis_started_at";
        ALTER TABLE "recorded_videos" DROP COLUMN "cm_analysis_error";
    """
