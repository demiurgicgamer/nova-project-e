import express from 'express';
import agoraToken from 'agora-token';
const { RtcTokenBuilder, RtcRole } = agoraToken;

const router = express.Router();

const APP_ID          = process.env.AGORA_APP_ID;
const APP_CERTIFICATE = process.env.AGORA_APP_CERTIFICATE;

// Token valid for 1 hour — enough for any single tutoring session
const TOKEN_EXPIRY_SECONDS = 3600;

/**
 * GET /api/agora/token?channel=<channelName>
 *
 * Returns a short-lived RTC token for the given channel.
 * Called by Unity before every JoinChannel().
 *
 * Requires: AGORA_APP_ID + AGORA_APP_CERTIFICATE in .env
 */
router.get('/token', (req, res) => {
    const channel = req.query.channel;

    if (!channel) {
        return res.status(400).json({ error: 'channel query param is required' });
    }

    if (!APP_ID || !APP_CERTIFICATE) {
        return res.status(503).json({
            error: 'Agora credentials not configured on server. Set AGORA_APP_ID and AGORA_APP_CERTIFICATE in .env',
        });
    }

    const expiresAt = Math.floor(Date.now() / 1000) + TOKEN_EXPIRY_SECONDS;

    const token = RtcTokenBuilder.buildTokenWithUid(
        APP_ID,
        APP_CERTIFICATE,
        channel,
        0,              // uid 0 = any user
        RtcRole.PUBLISHER,
        expiresAt,
        expiresAt,
    );

    console.log(`[Agora] Token generated — channel: ${channel}, expires in ${TOKEN_EXPIRY_SECONDS}s`);
    return res.json({ token, channel, expiresIn: TOKEN_EXPIRY_SECONDS });
});

export default router;
