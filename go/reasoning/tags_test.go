package reasoning

import (
	"strings"
	"testing"
)

// The expected tag names are spelled without their delimiters on purpose:
// the tags themselves are never typed literally (see tags.go).

func TestTagPairsCoverThinkThinkingAndBracketThink(t *testing.T) {
	pairs := TagPairs()
	if len(pairs) != 3 {
		t.Fatalf("TagPairs() has %d pairs, want 3", len(pairs))
	}
	want := []struct{ lead, name, trail string }{
		{"<", "think", ">"},
		{"<", "thinking", ">"},
		{"[", "THINK", "]"},
	}
	for i, w := range want {
		p := pairs[i]
		if p.Open != w.lead+w.name+w.trail {
			t.Errorf("pair %d Open = %q, want %q", i, p.Open, w.lead+w.name+w.trail)
		}
		if p.Close != w.lead+"/"+w.name+w.trail {
			t.Errorf("pair %d Close = %q, want %q", i, p.Close, w.lead+"/"+w.name+w.trail)
		}
	}
}

func TestTagConstantsMatchPairs(t *testing.T) {
	pairs := TagPairs()
	got := []TagPair{
		{ThinkOpen, ThinkClose},
		{ThinkingOpen, ThinkingClose},
		{BracketThinkOpen, BracketThinkClose},
	}
	for i := range got {
		if got[i] != pairs[i] {
			t.Errorf("constant pair %d = %+v, TagPairs()[%d] = %+v", i, got[i], i, pairs[i])
		}
	}
}

func TestTagPairsReturnsACopy(t *testing.T) {
	p := TagPairs()
	p[0].Open = "mutated"
	if TagPairs()[0].Open == "mutated" {
		t.Fatal("TagPairs() exposes shared state")
	}
}

func TestAdapterStripsEveryTagPair(t *testing.T) {
	a := New()
	for _, p := range TagPairs() {
		for _, open := range []string{p.Open, strings.ToUpper(p.Open)} {
			close := p.Close
			if open != p.Open {
				close = strings.ToUpper(p.Close)
			}
			raw := open + "\nplan the call\n" + close + "answer"
			clean, err := a.Strip(raw)
			if err != nil {
				t.Fatalf("%q: unexpected error: %v", open, err)
			}
			if clean != "answer" {
				t.Errorf("%q: clean = %q, want %q", open, clean, "answer")
			}
		}
	}
}

func TestAdapterFailsClosedOnUnclosedThink(t *testing.T) {
	if _, err := New().Strip(ThinkOpen + "never closed"); err == nil {
		t.Fatal("expected ErrUnbalancedThinking for an unclosed think tag")
	}
}
