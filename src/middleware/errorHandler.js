import { env } from '../config/env.js';

/**
 * Centralised Express error handler.
 * Must be registered last — after all routes.
 */
export const errorHandler = (err, req, res, _next) => {
    // Respect status codes set upstream (e.g. 400, 403, 409)
    const status = err.status || err.statusCode || 500;

    // Log full error in development, minimal in production
    if (env.isDev) {
        console.error(`[Error] ${req.method} ${req.path}`, err);
    } else {
        console.error(`[Error] ${req.method} ${req.path} — ${err.message}`);
    }

    res.status(status).json({
        error: status < 500
            ? err.message
            : env.isDev ? err.message : 'Internal server error',
    });
};

/** Wraps async route handlers — avoids try/catch boilerplate in every controller. */
export const asyncHandler = (fn) => (req, res, next) =>
    Promise.resolve(fn(req, res, next)).catch(next);
