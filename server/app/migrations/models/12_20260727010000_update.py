from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos"
            ADD COLUMN "cm_analysis_status" VARCHAR(255) NOT NULL DEFAULT 'Unanalyzed';
        UPDATE "recorded_videos"
            SET "cm_analysis_status" = CASE
                WHEN "cm_sections" IS NULL THEN 'Unanalyzed'
                ELSE 'Completed'
            END;
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "recorded_videos" DROP COLUMN "cm_analysis_status";
    """
