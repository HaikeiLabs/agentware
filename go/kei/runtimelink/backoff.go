package runtimelink

import (
	"context"
	"math"
	"time"
)

// maxBackoffShift keeps Min<<attempt from overflowing; any attempt past it is
// already capped at Max for every valid config.
const maxBackoffShift = 30

// Delay returns the full-jitter restart delay for attempt n (0-based):
// floor(r × min(Max, Min×2^n)) in whole milliseconds. r is a uniform sample
// clamped to [0, 1); negative attempts count as 0.
func (b Backoff) Delay(attempt int, r float64) time.Duration {
	attempt = min(max(attempt, 0), maxBackoffShift)
	capMS := min(b.Max.Milliseconds(), b.Min.Milliseconds()<<attempt)
	r = min(max(r, 0), math.Nextafter(1, 0))
	return time.Duration(math.Floor(r*float64(capMS))) * time.Millisecond
}

// Wait sleeps for Delay(attempt, r) or until ctx is done, whichever is first.
// It returns ctx.Err() when cancelled, so a Stop during backoff never waits
// out the delay.
func (b Backoff) Wait(ctx context.Context, attempt int, r float64) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	t := time.NewTimer(b.Delay(attempt, r))
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}
