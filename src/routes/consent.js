import { Router } from 'express';
import { requireAuth } from '../middleware/auth.js';
import { asyncHandler } from '../middleware/errorHandler.js';
import { recordConsent } from '../controllers/consentController.js';

const router = Router();

/**
 * POST /api/consent
 * Records parental consent timestamp for the authenticated parent.
 * Requires a valid backend JWT (parent must be registered first).
 */
router.post('/', requireAuth, asyncHandler(recordConsent));

export default router;
