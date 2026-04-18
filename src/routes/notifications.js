import { Router } from 'express';
import { requireAuth } from '../middleware/auth.js';
import { asyncHandler } from '../middleware/errorHandler.js';
import { registerToken } from '../services/NotificationService.js';

/**
 * /api/notifications
 *
 * POST /api/notifications/token
 *   Registers (or refreshes) the FCM push token for the authenticated parent.
 *   Unity calls this on every app launch after Firebase.Messaging.GetTokenAsync().
 *   Body: { fcmToken: string }
 */

const router = Router();

router.use(requireAuth);

router.post('/token', asyncHandler(async (req, res) => {
    const parentId = req.user.sub;
    const { fcmToken } = req.body;

    if (!fcmToken || typeof fcmToken !== 'string' || fcmToken.trim().length === 0)
        return res.status(400).json({ error: 'fcmToken is required.' });

    await registerToken(parentId, fcmToken.trim());

    return res.status(200).json({ success: true });
}));

export default router;
