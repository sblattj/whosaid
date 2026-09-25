# Action items — unnamed-asks

Speakers in this meeting: Tomasz_Example, Renata_Example, Kofi_Example, Lena_Example

_Auto-drafted 2026-09-24T17:05-07:00 by `claude-opus-5-5` (Claude, cloud: this transcript was sent to Anthropic) via `whosaid action-items --engine claude`: the whole transcript read in one call, 9 turns kept as evidence, 9 items drafted, 0 flagged ⚠. A DRAFT: read the evidence and the transcript before trusting it._

## 1. Asks from leadership (Renata)
- **Tomasz_Example** [Renata 00:01:29] Send Renata churn numbers broken out by signup month. Renata wants to see whether the churn increase is concentrated in the spring cohort; Tomasz agreed. "Could you also send me the numbers broken out by signup month?"
- **Tomasz_Example** [Renata 00:02:10] Add a footnote to the churn report about the March reactivation-flag double counting. The reactivation flag was only added in March, so earlier data is double counted and the March jump needs explaining. "please add a footnote about that in the report"
- **Tomasz_Example** [Renata 00:02:55] Investigate the hourly backfill job and report by Friday whether it can move to daily. The events pipeline bill rose about 18%, which Tomasz attributes to the hourly backfill job. "Can you dig into the backfill and tell me by Friday whether we can drop it to daily?"
- **Tomasz_Example** [Renata 00:03:41] Run the system design round for both data engineer candidates next week. Two candidates are on site next week; Tomasz agreed but is out Thursday. "Would you be able to run the system design round for both of them?"
- **Tomasz_Example** [Renata 00:04:32] Walk the execs through the churn methodology at the exec review. Renata wants the execs to hear it from Tomasz directly rather than from a slide; Tomasz agreed. "could you walk the execs through it yourself?"

## 2. Asks from team (Kofi, Lena)
- **Tomasz_Example** [Lena 00:01:43] Check whether trial conversions are double counted (reactivations counted as new trials). Lena suspects the dashboard counts reactivations as new trials; Tomasz noted pre-March data likely shows up twice. "can you check whether trial conversions are being double counted?"
- **Tomasz_Example** [Kofi 00:03:04] Give Kofi a heads up before changing the backfill job. The finance export reads from the backfill job; Tomasz agreed. "Before you change anything there, please give me a heads up"

## 3. Team directives from leadership (Renata)
- none

## 4. Tomasz's own commitments
- **Tomasz_Example** [Tomasz 00:04:08] Put the churn report query in the shared repo for Kofi. Kofi wants to reuse the query for retention cohort work. "Yeah, I'll put it in the shared repo."
- **Tomasz_Example** [Tomasz 00:04:18] Write a short churn methodology note before the exec review on the twentieth. The churn numbers are expected to draw questions at the exec review. "I'll write up a short note on the churn methodology before the exec review"

## 5. Inferred next steps
- **Tomasz_Example** [inferred] Tell the hiring coordinator that you are unavailable Thursday when the system design interviews are scheduled
- **Tomasz_Example** [inferred] Prepare streaming-focused system design questions for the candidate interviews
- **Tomasz_Example** [inferred] Correct or flag the dashboard's trial-conversion logic if the double counting is confirmed

<details><summary>Evidence turns the draft was built from (verbatim, 9)</summary>

- [00:01:29] Renata_Example: Interesting. Could you also send me the numbers broken out by signup month? I want to see if it's all the spring cohort.
- [00:01:43] Lena_Example: And while you're in there, can you check whether trial conversions are being double counted? I think the dashboard counts reactivations as new trials.
- [00:02:10] Renata_Example: Okay, then please add a footnote about that in the report, so nobody panics about the jump in March.
- [00:02:55] Renata_Example: Can you dig into the backfill and tell me by Friday whether we can drop it to daily?
- [00:03:04] Kofi_Example: Before you change anything there, please give me a heads up, the finance export reads from that job.
- [00:03:41] Renata_Example: Good. Would you be able to run the system design round for both of them? You're the obvious person for the streaming questions.
- [00:04:08] Tomasz_Example: Yeah, I'll put it in the shared repo.
- [00:04:18] Tomasz_Example: One more thing from me. I'll write up a short note on the churn methodology before the exec review on the twentieth, because those numbers are going to get questions.
- [00:04:32] Renata_Example: Please do. And could you walk the execs through it yourself? I'd rather they hear it from you than from a slide.

</details>
