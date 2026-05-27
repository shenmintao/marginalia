/** Chat session state — kept in memory across route changes.
 *
 *  ChatPage stores turns, streaming, loading, etc. in local useState,
 *  which gets lost when the user navigates away (e.g. clicks an `entry:`
 *  citation that opens the Library). Moving these into a Zustand store
 *  means the SSE stream keeps updating the store even after the component
 *  unmounts, so the user sees a live (or completed) conversation when they
 *  return.
 *
 *  sessionId is deliberately not persisted to localStorage: on next app
 *  launch we want the chat to start clean rather than auto-reload
 *  yesterday's session. Click a row in SessionList to resume any older
 *  session.
 */
import { create } from "zustand";
import type { Turn } from "@/components/TurnView";

interface ChatSessionState {
  sessionId: string | null;
  turns: Turn[];
  streaming: boolean;
  loading: boolean;
  setSessionId: (id: string | null) => void;
  setTurns: (updater: Turn[] | ((prev: Turn[]) => Turn[])) => void;
  setStreaming: (v: boolean) => void;
  setLoading: (v: boolean) => void;
  reset: () => void;
}

export const useChatSession = create<ChatSessionState>((set) => ({
  sessionId: null,
  turns: [],
  streaming: false,
  loading: false,
  setSessionId: (id) => set({ sessionId: id }),
  setTurns: (updater) =>
    set((state) => ({
      turns: typeof updater === "function" ? updater(state.turns) : updater,
    })),
  setStreaming: (v) => set({ streaming: v }),
  setLoading: (v) => set({ loading: v }),
  reset: () =>
    set({ sessionId: null, turns: [], streaming: false, loading: false }),
}));