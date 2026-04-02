import { Router } from 'express';
import { verifyFirebaseToken } from '../middleware/firebaseAuth.js';
import { requireRefreshToken } from '../middleware/auth.js';
import { authLimiter } from '../middleware/rateLimiter.js';
import { asyncHandler } from '../middleware/errorHandler.js';
import { register, login, refresh } from '../controllers/authController.js';

const router = Router();

// All auth routes share the strict rate limiter
router.use(authLimiter);

/**
 * POST /api/auth/register
 * Requires: Firebase ID token in Authorization header + { consentGranted: true } in body
 */
router.post('/register', verifyFirebaseToken, asyncHandler(register));

/**
 * POST /api/auth/login
 * Requires: Firebase ID token in Authorization header
 */
router.post('/login', verifyFirebaseToken, asyncHandler(login));

/**
 * POST /api/auth/refresh
 * Requires: { refreshToken } in body
 */
router.post('/refresh', requireRefreshToken, asyncHandler(refresh));

export default router;
