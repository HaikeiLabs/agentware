package reasoning

import (
	"strings"
	"testing"
)

// The expected tag names are spelled without their delimiters on purpose:
// the tags themselves are never typed literally (see tags.go).

func TestTagPairsCoverThinkMMThinkThinkingAndBracketThink(t *testing.T) {
	pairs := TagPairs()
	if len(pairs) != 4 {
		t.Fatalf("TagPairs() has %d pairs, want 4", len(pairs))
	}
	want := []struct{ lead, name, trail string }{
		{"<", "think", ">"},
		{"<", "mm:think", ">"},
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
		{MMThinkOpen, MMThinkClose},
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

func TestAdapterFailsClosedOnStrayMMThinkCloseByDefault(t *testing.T) {
	// MiniMax M2 emits the close tag without the open tag; without the opt-in
	// the adapter stays fail-closed.
	if _, err := New().Strip("reasoning" + MMThinkClose + "answer"); err == nil {
		t.Fatal("expected ErrUnbalancedThinking for a stray mm:think close tag")
	}
}

func TestAdapterImplicitOpenTreatsLeadingThinkCloseAsReasoning(t *testing.T) {
	clean, err := New(Options{ImplicitOpen: true}).Strip("reasoning" + ThinkClose + "answer")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if clean != "answer" {
		t.Errorf("clean = %q, want %q", clean, "answer")
	}
}

func TestAdapterImplicitOpenCountsMMThinkClose(t *testing.T) {
	an, err := New(Options{ImplicitOpen: true}).analyze("reasoning"+MMThinkClose+"answer", "")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if an.clean != "answer" || an.reasoning != "reasoning" {
		t.Errorf("clean = %q, reasoning = %q, want %q/%q", an.clean, an.reasoning, "answer", "reasoning")
	}
}

func TestAdapterImplicitOpenStillFailsClosedOnOtherCloseTags(t *testing.T) {
	// Close-only mode counts only the plain think and mm:think closes.
	if _, err := New(Options{ImplicitOpen: true}).Strip("reasoning" + ThinkingClose + "answer"); err == nil {
		t.Fatal("expected ErrUnbalancedThinking for a stray thinking close tag")
	}
}
