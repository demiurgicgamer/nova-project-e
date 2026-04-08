import { v4 as uuidv4 } from 'uuid';
import { query } from '../config/database.js';

const SUPPORTED_GRADES    = [6, 7];
const SUPPORTED_LANGUAGES = ['en', 'fr']; // Phase 1: Canada (EN + FR). Phase 2: ES, HI, AR.
const MAX_CHILDREN        = 5; // per parent account

// ── Helpers ───────────────────────────────────────────────────────────────────

const sanitizeChild = (row) => ({
    id:           row.id,
    parentId:     row.parent_id,
    name:         row.name,
    grade:        row.grade,
    languageCode: row.language_code,
    currentTopic: row.current_topic,
    weakTopics:   row.weak_topics,
    streakDays:   row.streak_days,
    totalSessions:row.total_sessions,
    totalStars:   row.total_stars,
    createdAt:    row.created_at,
});

// ── Controllers ───────────────────────────────────────────────────────────────

/**
 * POST /api/children
 * Creates a new child profile linked to the authenticated parent.
 * Body: { name, grade, languageCode }
 */
export const createChild = async (req, res) => {
    const parentId = req.user.sub;
    const { name, grade, languageCode = 'en' } = req.body;

    // Validate
    if (!name || typeof name !== 'string' || name.trim().length === 0)
        return res.status(400).json({ error: 'Child name is required.' });

    if (name.trim().length > 20)
        return res.status(400).json({ error: 'Name must be 20 characters or fewer.' });

    if (!SUPPORTED_GRADES.includes(Number(grade)))
        return res.status(400).json({ error: `Grade must be one of: ${SUPPORTED_GRADES.join(', ')}.` });

    if (!SUPPORTED_LANGUAGES.includes(languageCode))
        return res.status(400).json({ error: `Language must be one of: ${SUPPORTED_LANGUAGES.join(', ')}.` });

    // Check parent exists and consent is granted
    const parent = await query(
        'SELECT id, consent_date FROM parent_profiles WHERE id = $1',
        [parentId]
    );
    if (parent.rows.length === 0)
        return res.status(404).json({ error: 'Parent profile not found.' });

    if (!parent.rows[0].consent_date)
        return res.status(403).json({ error: 'Parental consent required before creating a child profile.' });

    // Enforce max children per parent
    const countResult = await query(
        'SELECT COUNT(*) FROM child_profiles WHERE parent_id = $1',
        [parentId]
    );
    if (parseInt(countResult.rows[0].count) >= MAX_CHILDREN)
        return res.status(400).json({ error: `Maximum of ${MAX_CHILDREN} child profiles per account.` });

    // Create profile
    const id = uuidv4();
    const result = await query(
        `INSERT INTO child_profiles (id, parent_id, name, grade, language_code)
         VALUES ($1, $2, $3, $4, $5)
         RETURNING *`,
        [id, parentId, name.trim(), Number(grade), languageCode]
    );

    return res.status(201).json(sanitizeChild(result.rows[0]));
};

/**
 * GET /api/children
 * Returns all child profiles for the authenticated parent.
 */
export const getChildren = async (req, res) => {
    const parentId = req.user.sub;

    const result = await query(
        'SELECT * FROM child_profiles WHERE parent_id = $1 ORDER BY created_at ASC',
        [parentId]
    );

    return res.status(200).json({ children: result.rows.map(sanitizeChild) });
};

/**
 * GET /api/children/:id
 * Returns a single child profile. Parent must own the child.
 */
export const getChild = async (req, res) => {
    const parentId = req.user.sub;
    const { id }   = req.params;

    const result = await query(
        'SELECT * FROM child_profiles WHERE id = $1 AND parent_id = $2',
        [id, parentId]
    );

    if (result.rows.length === 0)
        return res.status(404).json({ error: 'Child profile not found.' });

    return res.status(200).json(sanitizeChild(result.rows[0]));
};

/**
 * PUT /api/profile/language
 * Updates the language preference for the active child or parent.
 * Called by LanguageController after language selection.
 * Body: { languageCode }
 */
export const updateChildLanguage = async (req, res) => {
    const parentId = req.user.sub;
    const { languageCode, childId } = req.body;

    if (!SUPPORTED_LANGUAGES.includes(languageCode))
        return res.status(400).json({ error: 'Unsupported language code.' });

    if (childId) {
        // Update child language
        await query(
            'UPDATE child_profiles SET language_code = $1, updated_at = NOW() WHERE id = $2 AND parent_id = $3',
            [languageCode, childId, parentId]
        );
    }

    return res.status(200).json({ success: true });
};
