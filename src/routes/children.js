import { Router } from 'express';
import { requireAuth } from '../middleware/auth.js';
import { asyncHandler } from '../middleware/errorHandler.js';
import {
    createChild,
    getChildren,
    getChild,
    updateChild,
    deleteChild,
    updateChildLanguage,
} from '../controllers/childrenController.js';
import {
    getProgress,
    updateProgress,
} from '../controllers/progressController.js';
import {
    createSession,
    getSessions,
    patchChild,
} from '../controllers/sessionsController.js';

const router = Router();

// All children routes require a valid parent JWT
router.use(requireAuth);

/** POST /api/children              — create a child profile */
router.post('/',                              asyncHandler(createChild));

/** GET  /api/children              — list all children for this parent */
router.get('/',                               asyncHandler(getChildren));

/** GET    /api/children/:id          — get a single child profile */
router.get('/:id',                            asyncHandler(getChild));

/** PUT    /api/children/:id          — update name / grade / languageCode */
router.put('/:id',                            asyncHandler(updateChild));

/** DELETE /api/children/:id          — permanently delete child + all data */
router.delete('/:id',                         asyncHandler(deleteChild));

/** PATCH /api/children/:id         — update currentTopic / languageCode / weakTopics */
router.patch('/:id',                          asyncHandler(patchChild));

/** GET  /api/children/:id/progress — all topic progress for a child */
router.get('/:id/progress',                   asyncHandler(getProgress));

/** PUT  /api/children/:id/progress/:topicKey — upsert one topic's progress */
router.put('/:id/progress/:topicKey',         asyncHandler(updateProgress));

/** POST /api/children/:id/sessions — save a completed session */
router.post('/:id/sessions',                  asyncHandler(createSession));

/** GET  /api/children/:id/sessions — recent sessions list */
router.get('/:id/sessions',                   asyncHandler(getSessions));

export default router;
