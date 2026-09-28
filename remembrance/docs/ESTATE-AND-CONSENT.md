# Remembrance estate, consent, and stewardship framework

**Draft for qualified legal review — not legal advice or a verified compliance program.** Prepared 27 September 2026. Working assumption: a U.S. operator, with California as an example jurisdiction and European privacy issues flagged because hosting geography and visitor residence may cross borders. Hosting at Hetzner does not determine the governing law by itself. Confirm operator entity, server region, users’ locations, deceased persons’ domiciles, and service scope with counsel before accepting real families.

The app records declarations and supports moderation, access controls, reports, export, and erasure workflows. It does not verify probate documents, determine inheritance, register a nonprofit, supply an attorney, or automatically establish a legally effective digital-asset direction.

## 1. Keep distinct rights distinct

| Question | Evidence or decision to obtain | What the app does |
|---|---|---|
| Who may administer the memorial? | Living creator’s authorization; executor/trustee/representative documents where applicable; documented resolution of family disputes | Stores owner and typed authority declaration |
| Who may obtain a deceased person’s account data? | Applicable fiduciary-access law, account-provider process, deceased person’s directions, necessary documentation | No access to external accounts; no scraping |
| Who owns each photo, film, recording, or text? | Copyright ownership, written license, permission, or counsel-reviewed legal basis | Requires upload-rights confirmation, preserves chosen visibility |
| Who is depicted or discussed? | Privacy/publicity considerations; consent where required; special treatment for children and sensitive details | Child-media toggle defaults a public upload to family-only; reporting workflow |
| Who inherits memorial administration? | Valid applicable instructions and authority, identity verification, scope of permitted disclosure | Records account-bound nominations and acceptance; operator-reviewed transfer, with no automatic handover |
| Who maintains hosting and the printed URL? | Domain and infrastructure ownership, funded operating plan, succession credentials held securely | Deployable server, stable URLs, export and backup tools |

Authority to access an account is not blanket permission to publish it. Possessing a file is not ownership of its copyright. A funeral home or cemetery partnership does not confer estate or media rights.

## 2. Legal reference points to verify with counsel

**California digital assets.** Probate Code Part 20 addresses fiduciary access to digital assets. Section 873 provides for certain changeable online directions that may override contrary estate documents; section 874 does not enlarge the user’s underlying rights. Section 876 specifies documentation for disclosure of electronic-communication content. These are access/disclosure provisions, not universal publication licenses. The current successor email field is only an operational preference; do not present it as a compliant statutory online tool without counsel’s assessment and a distinct, appropriately documented direction mechanism. [Current California Probate Code §§870–884](https://leginfo.legislature.ca.gov/faces/codes_displayText.xhtml?division=2.&part=20.&lawCode=PROB).

**European privacy.** GDPR Recital 27 excludes deceased-person data while allowing Member State rules. Living relatives, visitors, contributors, and people appearing in media still create privacy obligations where GDPR applies; a memorial can mix both kinds of data. Evaluate territorial scope, lawful bases, controller/processor roles, data subject requests, sensitive data, children, and international transfers. Do not label all memorial content exempt “estate data.” [GDPR official text](https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32016R0679).

**California privacy.** Evaluate whether the operator meets CCPA applicability criteria, and which related laws apply even when it does not. Publish the actual data practices and a working request channel, rather than claiming compliance from the mere existence of an export button. [California Privacy Protection Agency: laws and regulations](https://privacy.ca.gov/laws-and-regulations/).

**Copyright reports.** U.S. section 512 safe harbors are conditional. Counsel should assess eligibility and requirements, including the appropriate notice/counter-notice process, agent designation, and repeat-infringer policy. A generic concern form alone does not establish safe-harbor protection. [U.S. Copyright Office: section 512 resources](https://www.copyright.gov/512/), [current 17 U.S.C. §512](https://uscode.house.gov/view.xhtml?req=%28title%3A17+section%3A512+edition%3Aprelim%29).

Publicity, biometric/voice, consumer-protection, contract, and probate rules vary. Counsel must check current applicable law before voice synthesis, international expansion, paid “permanent” service claims, or publication involving disputes or vulnerable people.

## 3. Proposed onboarding and authority review

These are recommended operating procedures, not automatically imposed legal requirements.

1. Offer a private draft first. Explain that becoming an owner does not authenticate estate status.
2. Record memorial subject, creator’s verified identity/contact, asserted role, authority basis, known wishes or restrictions, and policy version. Current web flow records name, role, time, and version-1 audit event; verification is manual.
3. Use a separate, restricted verification channel if probate or identity documents are needed. Do not upload death certificates or identity scans into the memorial gallery or public tribute system.
4. Confirm the minimum necessary scope: administer the profile, publish specified media, invite family, authorize imports, and/or nominate a successor. Avoid an unbounded “all rights forever” declaration.
5. If competing claims arise, restrict disputed content while a human reviews the evidence. Do not allow an uploaded certificate alone to trigger automatic takeover. Keep undisputed data available to entitled parties where appropriate.
6. Keep a decision record: case reference, reviewer, date, verified basis, permitted actions, restrictions, review deadline. Store supporting evidence separately with restricted access and a retention schedule.

The baseline app lets a declarant publish without operator verification. If your launch policy requires verification before every public memorial, add an enforced `authority_status` gate and an operator review interface before opening registration to the public. Until then, run an invited, manually reviewed pilot.

## 4. Draft memorial authorization form

Adapt before signature; do not present this draft as a legal instrument suitable for every jurisdiction.

- Memorial subject / stable ID: ____________________
- Creator’s full legal name and verified contact: ____________________
- Relationship and asserted legal role: ____________________
- Authority basis and private evidence reference: ____________________
- Relevant jurisdiction / deceased person’s domicile: ____________________
- Known directions, restrictions, disputes, or people to consult: ____________________
- Approved initial audience: only owner / named family / public
- Scope authorized: profile administration / listed content publication / specified imports / collaborator access
- Excluded content or actions: ____________________

**Proposed declaration:** “I confirm that the information I provide about my authority is accurate to the best of my knowledge. I authorize the operator to store and display the memorial content within the scope and audience I select, subject to applicable rights and law. I understand that this authorization does not grant rights I do not hold, does not override known lawful restrictions, and is not authorization for voice synthesis. I will promptly notify the operator of an authority dispute or material change.”

Typed/signed name, date/time, policy version, verified method: ____________________

Provide a copy and a straightforward correction/revocation process. Counsel should specify the appropriate electronic-signature evidence and retention practices. The current timestamped checkbox is a basic product record, not a representation that every execution requirement is met.

## 5. Draft per-asset rights and living-person record

Maintain a structured record outside the basic app for higher-risk assets:

- Asset ID / source / photographer, author, or rights holder.
- Rights basis and permission document reference; any attribution or usage restrictions.
- People identifiable; consent or other assessed lawful basis; sensitive information present.
- Child subjects and guardian authority where relevant; default to restricted sharing pending review.
- Approved audience, reproduction/export expectations, takedown contact, review date.

**Proposed permission:** “I authorize storage and display of the identified material in this memorial for the audience selected below. I retain my rights. This permission does not authorize advertising, model training, voice cloning, or unrelated commercial exploitation. I understand that previously distributed copies may not be recoverable, and I can request review through the stated contact channel.”

Have counsel resolve revocability, duration, compensation if any, contractual license terms, lawful retention, and treatment of copies. The app does not silently train models or clone voices.

## 6. Social imports and voice preservation

A lawful download from an external provider may still include third-party copyright, private correspondence, or living-person information. Import only selected, reviewed material under the provider’s rules and appropriate authority. Do not collect deceased-account passwords or scrape private accounts. Store minimal authorization evidence. The current release imports only the documented prepared JSON format, with private initial visibility.

Voice cloning is **not implemented**. Any future synthetic-voice feature needs a separate decision process covering deceased person’s documented wishes, representative authority, living speakers’ rights, jurisdictional rules, provider terms, permitted uses, identity verification, withdrawal limits, unmistakable synthetic labeling, and independent review. Do not bundle it into ordinary audio preservation consent. Preserve original recordings as original recordings.

## 7. Successor process and draft instruction

Legacy successor emails remain unverified preferences. The current [succession workflow](SUCCESSION-WORKFLOW.md) binds a nomination to a confirmed account, records acceptance or decline, and requires an operator-reviewed transfer. It gives no additional access at nomination or acceptance and never triggers from a death date. Contact the proposed successor directly; the application sends no email.

**Draft preference:** “If I die or become unable to administer this memorial, I prefer [name/contact] to be considered for continued stewardship, subject to verification of identity, applicable legal authority, restrictions I record, and the operator’s review process. Until that review is complete, this preference grants no access. My preferences concerning public, family-only, and private material are: [specific directions].”

For a legally operative disclosure tool, counsel must specify separate terms, modification/revocation at any time, record history, scope, recipient identity, conflicts, and applicable execution requirements. A person may wish private assets to be deleted or withheld rather than inherited by the named steward.

Operator workflow: open a private case; verify claimant identity and evidence; review account-holder directions and applicable legal documents; freeze disputed changes; determine minimum permissible disclosure; notify relevant parties as appropriate; record decision and appeal path; use the reviewed transfer command only after authorization; preserve the memorial ID. Never treat knowing the successor email address as sufficient proof.

## 8. Content, privacy, and erasure operations

Publish an actual monitored contact, operator legal identity, service address, and complaint process. Do not activate a fictional support mailbox. Check the report queue each business day during a pilot; route conflicts involving the owner directly to an independent operator.

Suggested operational targets (not claims about statutory deadlines): acknowledge within two business days, triage urgent exposure immediately, and track each case against counsel-confirmed applicable legal deadlines. Record category, scope, proof requested, reviewer, action, explanation, and closure date. Minimize verification data and avoid asking for unrelated identity documents.

- Ordinary family edits: archive and restore.
- Contributor withdrawal code: delete text/name from the live database; preserve only a noncontent action event.
- Memorial erasure: verify authority, consider legal hold and co-subject rights, then use the reviewed [erasure workflow](ERASURE-WORKFLOW.md), which removes live records and media files and records the decision in an off-database ledger. Account erasure still requires maintenance tooling. Restrict/expire backup copies according to published policy.
- Partial-media concern: owner can archive immediately; operator reviews redaction or permanent removal.
- Copyright: handle valid notices and counter-notices using counsel-approved procedure, not a generic automatic deletion rule.
- Restore after an incident: run `flask --app app erasure reapply` with the current off-host ledger before reopening service so a backup does not resurrect erased memorials.

The audit log is not exempt from privacy law. Do not copy erased content into it. Exported archives, screenshots, or material already published elsewhere cannot be recalled by this server.

## 9. Proposed retention schedule for operator adoption

| Data | Proposed pilot policy — publish only after implementation |
|---|---|
| Active account and memorial content | While service is requested, subject to erasure and lawful restrictions |
| Archived media | Retained until explicit deletion request or adopted lifecycle policy |
| Candle identifiers | 30 days; app purges on later candle submissions |
| Rate-limit digests | Short hour buckets, purged during later rate-limited requests |
| Web access logs | Disabled for this subdomain in supplied proxy examples; review infrastructure defaults |
| Server backups | Daily encrypted backups, 30-day rolling retention, tested restore; operator must configure |
| Identity/estate verification documents | Separate restricted store; minimum period justified by counsel, not indefinite gallery retention |
| Resolved reports and case records | Defined with counsel based on purpose and legal obligations; no silent forever policy |

No scheduled retention daemon for reports or external backups is bundled. Document responsibility and run the required jobs before asserting this schedule to families.

## 10. Entity and continuity decisions

Compare nonprofit, benefit corporation, conventional entity with binding stewardship commitments, and a partnership with an established memorial nonprofit. The software does not choose or incorporate an entity. Assess fiduciary responsibilities, charitable status/tax claims, governance, accounting, liability, insurance, and contracts with cemeteries/installers.

Do not promise “forever” without a funded and enforceable plan. A credible continuity package includes domain succession, designated infrastructure custodians, financial reserves, periodic portable exports, vendor exit provisions, a shutdown notification plan, and a separately negotiated archival partner. No escrow or nonprofit fallback exists merely because the app has an export button.

Suggested shutdown procedure: stop accepting new paid commitments; notify owners using a lawful, verified contact mechanism; provide an adequate export window and clear dates; offer a successor operator only with the required authority/consent; preserve or redirect physical URLs where funded and lawful; securely erase remaining data under the published schedule. Do not publish private archives to a public “open viewer” as a fallback.

## 11. Launch review package

Before real-family launch, counsel/operator should complete the entity/contact blanks; decide governing jurisdictions; approve terms, privacy notice, consent instruments, and copyright process; approve authority verification and dispute handling; review hosting region and processor agreements; test erasure/restore; adopt retention and incident-response responsibilities; review cemetery installation contracts; and remove any unsupported permanence claims. These are concrete gaps to close, not a statement that the pilot software is unusable for local evaluation.
