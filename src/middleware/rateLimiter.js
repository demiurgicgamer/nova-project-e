import rateLimit from 'express-rate-limit';

/**
 * Strict limiter for auth endpoints — prevents brute-force attacks.
 * 10 requests per 15 minutes per IP.
 */
export const authLimiter = rateLimit({
    windowMs:         15 * 60 * 1000,
    max:              10,
    standardHeaders:  true,
    legacyHeaders:    false,
    message:          { error: 'Too many requests. Please try again in 15 minutes.' },
    skipSuccessfulRequests: true, // only count failed attempts
});

/**
 * General API limiter for all other routes.
 * 100 requests per minute per IP.
 */
export const apiLimiter = rateLimit({
    windowMs:        60 * 1000,
    max:             100,
    standardHeaders: true,
    legacyHeaders:   false,
    message:         { error: 'Rate limit exceeded. Please slow down.' },
});
