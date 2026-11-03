------------------------------- MODULE SpecialistChildLifecycle -------------------------------
EXTENDS TLC
\* Child-session lifecycle for one specialist after kickoff has been submitted
\* and the work turn is running.
\*
\* Property (invariant Safe):
\*   A child turn that is still running is never recorded with exit_verified TRUE.
\*   A stall or absolute-cap finish never archives the opening text that was
\*   written before the summary prompt.
\*
\* Tool: TLC from the stable TLA+ release v1.7.4 (TLC 2.19 of 08 August 2024,
\* jar sha256 pinned by scripts/verify_specialist_lifecycle.py). Chosen because
\* the question is whether host events can interleave badly, and the steps are
\* the code's await points. No verifier here runs on this TypeScript.
\*
\* Variant:
\*   "current"      plugin/src/specialists.ts at the sync commit (PR #28)
\*   "pre28"        main 8a2c6c9: one absolute timer, sessionResult text archived,
\*                  no summary turn and no busy check (issue #27)
\*   "noBusyCheck"  current code with specialists.ts:574 removed
\*   "textSinceAll" current code, except textSince returns sessionResult of the
\*                  whole transcript instead of the slice after the summary id
\*                  (specialists.ts:195-199). The opening line is that result
\*                  when the summary turn did not end "succeeded" (no later
\*                  assistant text). A succeeded summary turn has written text
\*                  after the prompt, so sessionResult returns that text.
\*
\* Each action is one yield: host events may happen at an await, and the
\* statements between awaits run in the action that resumes. Line numbers are
\* the current file unless marked pre28.
\*
\* The opening line from issue #27 is the transcript's last assistant text until
\* a summary turn ends "succeeded". An interrupted summary turn adds no
\* assistant text. A correct textSince never returns the opening line.

CONSTANT Variant

ASSUME Variant \in {"current", "pre28", "noBusyCheck", "textSinceAll"}

VARIABLES
  pc,              \* which await the plugin is blocked in
  running,         \* ground truth: a child turn is in progress
  outcome,         \* "none" | "succeeded" | "interrupted" once the turn has ended
  limit,           \* "none" | "stall" | "cap"  (pre28 uses only "cap")
  parentAbort,     \* delegate AbortSignal
  fromAbort,       \* summarize()'s fromAbort: parent, not the grace timer
  quiet,           \* stall window has elapsed since the last progress event
  limitsArmed,     \* armLimits timer still scheduled
  summaryLanded,   \* summary message id is in the transcript
  sendStarted,     \* host.synthetic for the summary has been called
  sendSettled,     \* that promise resolved or rejected; it can no longer land
  exitedSeen,      \* last finished()/stop() result
  willVerify,      \* exit_verified value the next ledger write will store
  badArchive,      \* that write archives the pre-summary opening line
  recorded,        \* specialist_finish has returned
  exitVerified,    \* recorded exit_verified
  archivedPre,     \* recorded archive used the opening line on a limit finish
  ret,             \* who called stop(): "work" | "dead" | "sum" | "throw"
  sumAborted       \* summary grace controller aborted

\* Opening line from issue #27 is already in the transcript. A limit finish
\* must not archive it. There is no summary prompt on main at 8a2c6c9.

vars == <<pc, running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
          summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
          recorded, exitVerified, archivedPre, ret, sumAborted>>

Init ==
  /\ pc = "Watch"
  /\ running = TRUE
  /\ outcome = "none"
  /\ limit = "none"
  /\ parentAbort = FALSE
  /\ fromAbort = FALSE
  /\ quiet = TRUE
  /\ limitsArmed = TRUE
  /\ summaryLanded = FALSE
  /\ sendStarted = FALSE
  /\ sendSettled = FALSE
  /\ exitedSeen = FALSE
  /\ willVerify = FALSE
  /\ badArchive = FALSE
  /\ recorded = FALSE
  /\ exitVerified = FALSE
  /\ archivedPre = FALSE
  /\ ret = "work"
  /\ sumAborted = FALSE

\* ---- host and timers -------------------------------------------------------

\* index.ts:432 specialists.noteProgress; specialists.ts:251-253.
\* Status heartbeats are not progress (specialistProgressEvent, specialists.ts:76-79).
ProgressResetsStall ==
  /\ Variant # "pre28"
  /\ pc = "Watch"
  /\ limitsArmed
  /\ quiet
  /\ quiet' = FALSE
  /\ UNCHANGED <<pc, running, outcome, limit, parentAbort, fromAbort, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, ret, sumAborted>>

\* The stall window can elapse again after progress. specialists.ts:475-478.
QuietWindowElapses ==
  /\ Variant # "pre28"
  /\ pc = "Watch"
  /\ limitsArmed
  /\ ~quiet
  /\ quiet' = TRUE
  /\ UNCHANGED <<pc, running, outcome, limit, parentAbort, fromAbort, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, ret, sumAborted>>

\* specialists.ts:470-473. The current tick and its mark callback return without
\* marking once the work controller is already aborted, so a parent abort
\* suppresses the cap. pre28's timer at 8a2c6c9:252-255 sets timedOut and
\* aborts with no signal.aborted check, so it still fires after the parent.
CapTimerAborts ==
  /\ limitsArmed
  /\ limit = "none"
  /\ \/ Variant = "pre28"
     \/ ~parentAbort
  /\ limit' = "cap"
  /\ UNCHANGED <<pc, running, outcome, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, ret, sumAborted>>

\* specialists.ts:475-478. pre28 has no stall timer.
StallTimerAborts ==
  /\ Variant # "pre28"
  /\ limitsArmed
  /\ quiet
  /\ limit = "none"
  /\ ~parentAbort
  /\ limit' = "stall"
  /\ UNCHANGED <<pc, running, outcome, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, ret, sumAborted>>

\* Parent abort listener aborts the work controller. specialists.ts:381-383.
\* During the summary send it also sets fromAbort and the summary deadline
\* (specialists.ts:533-536, 557).
ParentAborts ==
  /\ ~parentAbort
  /\ \/ /\ pc = "Watch"
        /\ parentAbort' = TRUE
        /\ UNCHANGED <<pc, running, outcome, limit, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                      recorded, exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ pc = "ReadTranscript"
        /\ parentAbort' = TRUE
        /\ UNCHANGED <<pc, running, outcome, limit, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                      recorded, exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ pc = "SumSend"
        /\ parentAbort' = TRUE
        /\ fromAbort' = TRUE
        /\ pc' = "StopInterrupt"
        /\ ret' = "dead"
        /\ UNCHANGED <<running, outcome, limit, quiet, limitsArmed, summaryLanded,
                      sendStarted, sendSettled, exitedSeen, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, sumAborted>>
     \/ /\ pc = "SumWatch"
        /\ parentAbort' = TRUE
        /\ fromAbort' = TRUE
        /\ sumAborted' = TRUE
        /\ UNCHANGED <<pc, running, outcome, limit, quiet, limitsArmed, summaryLanded,
                      sendStarted, sendSettled, exitedSeen, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret>>

\* Host ends the current turn. session.wait resolves only from this (the test
\* host: wait resolves when the session is not busy).
TurnEnds(kind) ==
  /\ running
  /\ pc \in {"Watch", "StopInterrupt", "StopGrace", "ReadTranscript", "SumSend",
             "SumWatch", "DeadRead", "BusyProbe", "PostGraceRead", "BoundedRead"}
  /\ running' = FALSE
  /\ outcome' = kind
  /\ UNCHANGED <<pc, limit, parentAbort, fromAbort, quiet, limitsArmed, summaryLanded,
                sendStarted, sendSettled, exitedSeen, willVerify, badArchive, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

TurnEndsSucceeded == TurnEnds("succeeded")
TurnEndsInterrupted == TurnEnds("interrupted")

\* Summary synthetic is accepted: the message id is stored and the turn starts
\* in one host step (plugin/test fake host synthetic). The plugin's promise may
\* still be pending. specialists.ts:556.
SummarySendLandsAndStartsTurn ==
  /\ sendStarted
  /\ ~sendSettled
  /\ ~summaryLanded
  /\ summaryLanded' = TRUE
  /\ running' = TRUE
  /\ UNCHANGED <<pc, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                sendStarted, sendSettled, exitedSeen, willVerify, badArchive, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

\* ---- work turn: finished() then maybe stop() -------------------------------

\* specialists.ts:497-499. pre28: 8a2c6c9:357-359.
WatchSeesIdle ==
  /\ pc = "Watch"
  /\ ~running
  /\ exitedSeen' = TRUE
  /\ pc' = "ReadTranscript"
  /\ limitsArmed' = (Variant = "pre28")
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, summaryLanded,
                sendStarted, sendSettled, willVerify, badArchive, recorded, exitVerified, archivedPre,
                ret, sumAborted>>

\* specialists.ts:493-501. Abort wins the race; stop() uses the same wait.
WatchSeesAbort ==
  /\ pc = "Watch"
  /\ limit # "none" \/ parentAbort
  /\ pc' = "StopInterrupt"
  /\ ret' = "work"
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, sumAborted>>

\* specialists.ts:506. Interrupt RPC returned and the turn stopped.
InterruptStopsTheTurn ==
  /\ pc = "StopInterrupt"
  /\ running
  /\ running' = FALSE
  /\ outcome' = "interrupted"
  /\ pc' = "StopGrace"
  /\ UNCHANGED <<limit, parentAbort, fromAbort, quiet, limitsArmed, summaryLanded,
                sendStarted, sendSettled, exitedSeen, willVerify, badArchive, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

\* specialists.ts:506 `.catch`: the RPC returned and the turn kept going.
InterruptReturnsWithoutStopping ==
  /\ pc = "StopInterrupt"
  /\ pc' = "StopGrace"
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, ret, sumAborted>>

\* Shared tail of stop()'s grace race. specialists.ts:508-513.
\* ret selects the caller: work finished, deadline stop, summary finished, or catch.
StopGraceSeesIdle ==
  /\ pc = "StopGrace"
  /\ ~running
  /\ exitedSeen' = TRUE
  /\ \/ /\ ret = "work"
        /\ pc' = "ReadTranscript"
        /\ limitsArmed' = (Variant = "pre28")
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet,
                      summaryLanded, sendStarted, sendSettled, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ ret = "dead"
        /\ pc' = "DeadRead"
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ ret = "throw"
        /\ pc' = "Settle"
        /\ willVerify' = TRUE
        /\ badArchive' = FALSE
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, recorded, exitVerified, archivedPre,
                      ret, sumAborted>>
     \/ /\ ret = "sum" /\ ~sumAborted
        /\ pc' = "BoundedRead"
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ ret = "sum" /\ sumAborted /\ fromAbort
        /\ pc' = "Settle"
        /\ willVerify' = TRUE
        /\ badArchive' = FALSE
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, recorded, exitVerified, archivedPre,
                      ret, sumAborted>>
     \/ /\ ret = "sum" /\ sumAborted /\ ~fromAbort
        /\ pc' = "PostGraceRead"
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret, sumAborted>>

StopGraceExpires ==
  /\ pc = "StopGrace"
  /\ exitedSeen' = FALSE
  /\ \/ /\ ret \in {"work", "dead", "throw", "sum"}
        /\ pc' = IF ret = "work" THEN "ReadTranscript"
                 ELSE IF ret = "dead" THEN "DeadRead"
                 ELSE "Settle"
        /\ willVerify' = FALSE
        /\ badArchive' = FALSE
        /\ limitsArmed' = IF ret = "work" /\ Variant = "pre28" THEN TRUE ELSE FALSE
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet,
                      summaryLanded, sendStarted, sendSettled, recorded, exitVerified, archivedPre,
                      ret, sumAborted>>

\* ---- finishChild's message read --------------------------------------------

\* specialists.ts:611-613. pre28 archives sessionResult text at 8a2c6c9:294-318.
\* found outcome is "none" when the read happens while the turn is still running
\* (no idle message yet). specialists.ts:169-178 / pre28:113-126.
ReadTranscriptAndDecide ==
  /\ pc = "ReadTranscript"
  /\ LET found == IF running THEN "none" ELSE outcome
     IN \/ /\ Variant = "pre28"
           /\ pc' = "Settle"
           /\ willVerify' = exitedSeen
           /\ badArchive' = (limit # "none" /\ found # "succeeded")
           /\ limitsArmed' = FALSE
           /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet,
                         summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                         archivedPre, ret, sumAborted>>
        \/ /\ Variant # "pre28"
           /\ limit # "none"
           /\ exitedSeen
           /\ found # "succeeded"
           /\ parentAbort
           /\ pc' = "Settle"
           /\ willVerify' = TRUE
           /\ badArchive' = FALSE
           /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                         summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                         archivedPre, ret, sumAborted>>
        \/ /\ Variant # "pre28"
           /\ limit # "none"
           /\ exitedSeen
           /\ found # "succeeded"
           /\ ~parentAbort
           /\ pc' = "SumSend"
           /\ sendStarted' = TRUE
           /\ badArchive' = FALSE
           /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                         summaryLanded, sendSettled, exitedSeen, willVerify, recorded, exitVerified,
                         archivedPre, ret, sumAborted>>
        \/ /\ Variant # "pre28"
           /\ ~(limit # "none" /\ exitedSeen /\ found # "succeeded")
           /\ pc' = "Settle"
           /\ willVerify' = exitedSeen
           /\ badArchive' = FALSE
           /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                         summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                         archivedPre, ret, sumAborted>>

\* ---- summarize() ------------------------------------------------------------

\* specialists.ts:556-557. The promise resolves only after the host has accepted
\* the message, so the land action has already run.
SummarySendSettles ==
  /\ pc = "SumSend"
  /\ summaryLanded
  /\ pc' = "SumWatch"
  /\ sumAborted' = FALSE
  /\ exitedSeen' = FALSE
  /\ sendSettled' = TRUE
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, willVerify, badArchive, recorded,
                exitVerified, archivedPre, ret>>

\* specialists.ts:591. The send rejected. Catch calls stop() and keeps its result.
SummarySendThrows ==
  /\ pc = "SumSend"
  /\ pc' = "StopInterrupt"
  /\ ret' = "throw"
  /\ sendSettled' = TRUE
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, sumAborted>>

\* specialists.ts:538 and 557. Grace wins the race with the unsettled send.
SummaryGraceExpires ==
  /\ pc = "SumSend"
  /\ pc' = "StopInterrupt"
  /\ ret' = "dead"
  /\ fromAbort' = FALSE
  /\ UNCHANGED <<running, outcome, limit, parentAbort, quiet, limitsArmed, summaryLanded,
                sendStarted, sendSettled, exitedSeen, willVerify, badArchive, recorded, exitVerified,
                archivedPre, sumAborted>>

\* specialists.ts:580. finished() on the summary controller.
SummaryWatchSeesIdle ==
  /\ pc = "SumWatch"
  /\ ~running
  /\ exitedSeen' = TRUE
  /\ \/ /\ ~sumAborted
        /\ pc' = "BoundedRead"
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ sumAborted /\ fromAbort
        /\ pc' = "Settle"
        /\ willVerify' = TRUE
        /\ badArchive' = FALSE
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, recorded, exitVerified, archivedPre,
                      ret, sumAborted>>
     \/ /\ sumAborted /\ ~fromAbort
        /\ pc' = "PostGraceRead"
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, willVerify, badArchive, recorded,
                      exitVerified, archivedPre, ret, sumAborted>>

\* Grace or parent wins; finished() calls stop() with the same wait. specialists.ts:501.
SummaryGraceDuringWatch ==
  /\ pc = "SumWatch"
  /\ ~sumAborted
  /\ ~parentAbort
  /\ sumAborted' = TRUE
  /\ UNCHANGED <<pc, running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, ret>>

SummaryWatchSeesAbort ==
  /\ pc = "SumWatch"
  /\ sumAborted
  /\ pc' = "StopInterrupt"
  /\ ret' = "sum"
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                recorded, exitVerified, archivedPre, sumAborted>>

\* specialists.ts:566-578. The read either sees the summary id or times out empty.
\* A miss records exited false (the late send is stopped when it settles, and
\* that follow-up stop does not decide the recorded exit).
DeadReadReturns ==
  /\ pc = "DeadRead"
  /\ \/ /\ ~(summaryLanded /\ exitedSeen)
        /\ pc' = "Settle"
        /\ willVerify' = FALSE
        /\ badArchive' = FALSE
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                      archivedPre, ret, sumAborted>>
     \/ /\ summaryLanded
        /\ exitedSeen
        /\ Variant \in {"current", "textSinceAll"}
        /\ pc' = "BusyProbe"
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, badArchive,
                      recorded, exitVerified, archivedPre, ret, sumAborted>>
     \/ /\ summaryLanded
        /\ exitedSeen
        /\ Variant = "noBusyCheck"
        /\ pc' = "Settle"
        /\ willVerify' = TRUE
        /\ badArchive' = FALSE
        /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                      summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                      archivedPre, ret, sumAborted>>

\* specialists.ts:574. childBusy true -> do not claim a verified exit.
\* index.ts:276-286: a probe that hangs, times out, or throws is not proof of idle
\* (within at index.ts:30-34 returns undefined; childBusy's probeWithin at
\* specialists.ts:186-192 and 282 substitutes TRUE). A completed probe reports
\* the running bit at this step.
BusyProbeSeesRunningChild ==
  /\ pc = "BusyProbe"
  /\ running
  /\ pc' = "Settle"
  /\ willVerify' = FALSE
  /\ badArchive' = FALSE
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

BusyProbeSeesIdleChild ==
  /\ pc = "BusyProbe"
  /\ ~running
  /\ pc' = "Settle"
  /\ willVerify' = TRUE
  /\ badArchive' = FALSE
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

BusyProbeHangsOrTimesOut ==
  /\ pc = "BusyProbe"
  /\ pc' = "Settle"
  /\ willVerify' = FALSE
  /\ badArchive' = FALSE
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

\* specialists.ts:582-590. textSince (195-199) drops text before the summary id.
\* The read may time out (probeWithin) or the bounded read may hit the grace.
\* Both store exited true. finishChild archives a non-empty summary.text
\* (625-645). On current that text is not the opening line, so badArchive
\* stays FALSE (an empty slice takes the preview "" branch and also does not
\* archive it). textSinceAll returns sessionResult of every message: the
\* opening line when the summary turn did not end succeeded, and finishChild
\* archives that non-empty line. A succeeded summary turn is later text.
PostGraceReadSettles ==
  /\ pc = "PostGraceRead"
  /\ pc' = "Settle"
  /\ willVerify' = TRUE
  /\ badArchive' = (Variant = "textSinceAll" /\ outcome # "succeeded")
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

BoundedSummaryReadSettles ==
  /\ pc = "BoundedRead"
  /\ pc' = "Settle"
  /\ willVerify' = TRUE
  /\ badArchive' = (Variant = "textSinceAll" /\ outcome # "succeeded")
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, recorded, exitVerified,
                archivedPre, ret, sumAborted>>

\* specialists.ts:706-710 specialist_finish. exit_verified is the value chosen
\* above (621, 635, 650, 667). This action is the ledger write.
RecordLedger ==
  /\ pc = "Settle"
  /\ pc' = "Done"
  /\ recorded' = TRUE
  /\ exitVerified' = willVerify
  /\ archivedPre' = badArchive
  /\ badArchive' = badArchive
  /\ UNCHANGED <<running, outcome, limit, parentAbort, fromAbort, quiet, limitsArmed,
                summaryLanded, sendStarted, sendSettled, exitedSeen, willVerify, ret, sumAborted>>

DoneStutter ==
  /\ pc = "Done"
  /\ UNCHANGED vars

Next ==
  \/ ProgressResetsStall
  \/ QuietWindowElapses
  \/ CapTimerAborts
  \/ StallTimerAborts
  \/ ParentAborts
  \/ TurnEndsSucceeded
  \/ TurnEndsInterrupted
  \/ SummarySendLandsAndStartsTurn
  \/ WatchSeesIdle
  \/ WatchSeesAbort
  \/ InterruptStopsTheTurn
  \/ InterruptReturnsWithoutStopping
  \/ StopGraceSeesIdle
  \/ StopGraceExpires
  \/ ReadTranscriptAndDecide
  \/ SummarySendSettles
  \/ SummarySendThrows
  \/ SummaryGraceExpires
  \/ SummaryWatchSeesIdle
  \/ SummaryGraceDuringWatch
  \/ SummaryWatchSeesAbort
  \/ DeadReadReturns
  \/ BusyProbeSeesRunningChild
  \/ BusyProbeSeesIdleChild
  \/ BusyProbeHangsOrTimesOut
  \/ PostGraceReadSettles
  \/ BoundedSummaryReadSettles
  \/ RecordLedger
  \/ DoneStutter

\* Still running => the ledger must not say the exit was verified.
\* A stall or cap must not archive the opening line from before the summary.
\* The PrintT conjunct is true on every state. TLC evaluates it only on the
\* initial state, so a successful run's output contains this sentence only
\* when Safe itself was checked.
Safe ==
  /\ ~(recorded /\ exitVerified /\ running)
  /\ ~archivedPre
  /\ IF /\ pc = "Watch"
        /\ running
        /\ outcome = "none"
        /\ limit = "none"
        /\ ~parentAbort
        /\ quiet
        /\ limitsArmed
        /\ ~summaryLanded
        /\ ~sendStarted
        /\ ~recorded
     THEN PrintT("Checked invariant Safe")
     ELSE TRUE

=============================================================================
