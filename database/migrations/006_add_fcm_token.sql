-- Migration 006: add FCM push token to parent_profiles
-- Run after 005_create_progress_and_achievements.sql
--
-- Each parent device registers its FCM token on login.
-- Notifications (session complete, streak milestones, daily reminders)
-- are sent to this token via Firebase Admin SDK.
-- A parent who reinstalls the app will get a new token — the UPSERT in
-- NotificationService always overwrites with the latest token.

ALTER TABLE parent_profiles
    ADD COLUMN IF NOT EXISTS fcm_token TEXT;

-- Optional index for bulk-send queries (daily reminders, milestone events)
CREATE INDEX IF NOT EXISTS idx_parent_profiles_fcm_token
    ON parent_profiles (fcm_token)
    WHERE fcm_token IS NOT NULL;
