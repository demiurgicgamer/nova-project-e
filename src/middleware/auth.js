import jwt from 'jsonwebtoken';
import { env } from '../config/env.js';

/**
 * Verifies the backend-issued JWT on protected routes.
 * Attaches decoded payload to req.user on success.
 */
export const requireAuth = (req, res, next) => {
    const header = req.headers.authorization;
    if (!header?.startsWith('Bearer ')) {
        return res.status(401).json({ error: 'Missing or invalid Authorization header' });
    }

    const token = header.slice(7);
    try {
        req.user = jwt.verify(token, env.auth.jwtSecret);
        next();
    } catch (err) {
        const message = err.name === 'TokenExpiredError' ? 'Token expired' : 'Invalid token';
        return res.status(401).json({ error: message });
    }
};

/**
 * Verifies the refresh token and attaches payload to req.user.
 * Used exclusively on POST /auth/refresh.
 */
export const requireRefreshToken = (req, res, next) => {
    const { refreshToken } = req.body;
    if (!refreshToken) {
        return res.status(401).json({ error: 'Refresh token required' });
    }

    try {
        req.user = jwt.verify(refreshToken, env.auth.jwtRefreshSecret);
        next();
    } catch (err) {
        const message = err.name === 'TokenExpiredError' ? 'Refresh token expired' : 'Invalid refresh token';
        return res.status(401).json({ error: message });
    }
};

/** Generates a short-lived access token (24h). */
export const signAccessToken = (payload) =>
    jwt.sign(payload, env.auth.jwtSecret, { expiresIn: env.auth.jwtExpiresIn });

/** Generates a long-lived refresh token (30d). */
export const signRefreshToken = (payload) =>
    jwt.sign(payload, env.auth.jwtRefreshSecret, { expiresIn: env.auth.refreshExpiresIn });
