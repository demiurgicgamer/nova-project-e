import { Router } from 'express';
import { requireAuth } from '../middleware/auth.js';
import { asyncHandler } from '../middleware/errorHandler.js';
import {
    createChild,
    getChildren,
    getChild,
    updateChildLanguage,
} from '../controllers/childrenController.js';

const router = Router();

// All children routes require a valid parent JWT
router.use(requireAuth);

/** POST /api/children — create a child profile */
router.post('/',     asyncHandler(createChild));

/** GET  /api/children — list all children for this parent */
router.get('/',      asyncHandler(getChildren));

/** GET  /api/children/:id — get a single child profile */
router.get('/:id',   asyncHandler(getChild));

export default router;
