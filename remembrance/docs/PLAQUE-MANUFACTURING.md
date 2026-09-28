# Remembrance QR / NFC memorial marker — engineering specification, revision A

Status: prototype procurement and acceptance specification. Do not engrave production markers until the HTTPS domain is live, the generated code is scanned from multiple devices, the cemetery approves installation, and the family approves the exact proof. Dimensions below are proposed design requirements; no environmental certification, supplier quote, or life warranty has been obtained.

## 1. Marker configurations

| Variant | Proposed construction | Intended use |
|---|---|---|
| Standard | 70 × 90 × 1.5 mm 316L stainless substrate, rounded 3 mm corners, matte opaque white marking field with black image | Mechanically fastened outdoor marker |
| Ceramic | 70 × 90 mm glazed outdoor ceramic, fired high-contrast decoration, protected perimeter | Recessed or carrier-mounted marker; supplier to establish thickness |
| Keepsake | Paper print generated in-app at 90 × 125 mm | Indoor proof, ceremony program, memory box; not outdoor rated |
| Hybrid | Standard or ceramic marker plus an isolated, outdoor-rated NFC assembly | QR + NFC + readable fallback URL |

A 50 × 50 mm product is an optional compact variant only after module-size calculations and scan testing show it is sufficient. It is not the default: the full URL can make an H-level QR too dense at that size.

## 2. Artwork and data contract

1. Encode exactly `https://remembrance.astra-arcana.com/m/<immutable-id>` once that origin is commissioned. Do not encode localhost, a staging host, a URL shortener, an analytics redirect, or a link with an expiry/signature.
2. Use the app’s generated SVG. H-level error correction; black square modules on an opaque white field; no decorative logo, rounded modules, gradients, cutouts, or inverse polarity.
3. Preserve a clear zone at least **four modules on every side**. The SVG includes this border. Text, screw heads, adhesive edges, frames, and printed ornament must remain outside it.
4. Target **0.8 mm or greater module pitch** outdoors. Compute required printed width as `(matrix modules + 8) × module pitch`; the eight accounts for both four-module margins. Example only: a 49-module matrix needs at least 45.6 mm including quiet zone at 0.8 mm pitch. Inspect the actual generated version; do not assume all URLs create that matrix size.
5. Reserve approximately 54 × 54 mm within the 70 × 90 mm marker for the full QR plus its quiet zone. If the actual code does not meet pitch requirements there, enlarge the marker or adopt a shorter operator-owned durable origin **before manufacturing**. Never squeeze modules to fit.
6. Print the canonical URL in readable text under the QR; wrap only at a slash between path components. Never hyphenate or insert spaces into a transcription. At small sizes a separately managed human-readable alias can be added, but the current application implements the canonical `/m/id` route only.
7. Suggested wording: “Scan to visit their memory.” Add NFC “Tap” only where hardware is fitted and tested. Optional name/dates must exactly match the family-approved proof.

The app’s SVG is a QR production asset. Its print page is a paper keepsake, not a full machining drawing. Request a dimensioned CAD proof from the selected manufacturer showing full quiet zone, mounting clearance, type size, and material stack.

DENSO WAVE specifies four-module margins and explains that higher error correction increases code size. Level H is commonly described as restoring roughly 30% of codewords; this is **not a guarantee that any arbitrary 30% of the physical surface may be removed**. Damage to finder patterns, poor contrast, glare, or the quiet zone may prevent reading. Sources: [QR margins](https://www.qrcode.com/en/howto/code.html), [error correction](https://www.qrcode.com/en/about/error_correction.html).

## 3. Marking process and surface

Supplier must demonstrate black-on-white contrast in shade, sun, wet conditions, and oblique views. Bare reflective steel with subtle laser discoloration is not accepted without passing the optical tests. Specify a compatible durable opaque background and marking process (e.g. supplier-qualified enamel/ceramic coating), or a ceramic construction with fired decoration. Require evidence that the coating and dark mark survive the proposed outdoor exposure and cleaning process.

Edges must be deburred and corners rounded. No sharp burrs or exposed peelable paper laminate. The readable code surface must be flat and matte. Ask suppliers to document production tolerances and reject any module edge damage that materially changes the generated geometry.

Do not market “10–25 year life” on material name alone. Obtain the supplier’s actual warranty, exposure results, corrosion/UV/abrasion data, exclusions, and expected maintenance regime. Accelerated weathering results do not translate automatically into a fixed real-world life.

## 4. Mounting

Get written landowner/cemetery permission and monument conservator approval before drilling, bonding, or altering a headstone. Do not instruct families to drill fragile stone. Have a qualified monument installer choose attachment appropriate to the substrate, preservation requirements, climate, and accessibility.

- Mechanical: supplier-designed holes outside the QR field, corrosion-compatible fasteners and isolation washers, hidden sharp ends, allowance for thermal movement. Trial proposed hole locations on the full-size proof before cutting.
- Adhesive: outdoor-rated system expressly compatible with the exact stone/coating and climate. Installer follows manufacturer cleaning, cure, temperature, and load instructions. Test for staining and removal damage on a sample. No “universal permanent adhesive” claim.
- Recessed: monument fabricator controls recess depth, drainage, sealant compatibility, and serviceability.

Avoid horizontal dirt traps, pooled water, landscaping spray, direct mower contact, and locations routinely covered by offerings or flowers. Choose a respectful viewing position accessible from the path. Record location privately when public disclosure could cause harm.

## 5. Optional NFC

Use a phone-compatible NFC Forum Type 2 or Type 4 tag storing a single NDEF URI record containing the **same canonical HTTPS URL** as the QR. Require outdoor encapsulation and supplier temperature/UV/moisture specifications. Metal-mounted tags need a supplier-qualified ferrite-backed/on-metal assembly with tuned antenna spacing; a generic sticker directly on steel may not read.

Test on the actual assembled marker, not a loose tag. Verify first, then permanently lock the URI record if the selected tag supports it and the procurement policy calls for locking. Store lot ID and lock state in the manufacturing record. A replacement tag should not require a new memorial URL. QR remains the primary universally visible access method; NFC support varies by device.

## 6. Prototype acceptance plan

Produce at least 10 prototypes spanning each material/process/attachment variant; the exact sample size and formal inspection standard should be agreed with the manufacturer. Suggested engineering acceptance tests:

| Test | Procedure | Proposed acceptance |
|---|---|---|
| Digital identity | Decode SVG/PNG with two independent decoders; compare URL byte-for-byte with release record | Exact match; HTTPS profile resolves to correct person |
| Print geometry | Measure module pitch and all quiet zones on physical sample | Meets section 2; no clipped or filled gaps |
| Ordinary scan | Native cameras on at least two iPhones and three Android phones of different ages; 20/30/50 cm, portrait and landscape | Correct destination in ≤3 seconds at intended 30 cm distance in all ordinary conditions; record other ranges |
| Angle and light | Head-on and ±30°; shade, bright sun, wet surface; document lux where practical | Readable at intended distance without touching or cleaning under ordinary conditions |
| Mounted assembly | Repeat tests after actual mounting, adhesive cure, and NFC installation | No degradation due to glare, recess, frame, metal, or tag interference |
| Environmental | Supplier-agreed UV, salt/corrosion, freeze/thaw, abrasion and cleaning trials relevant to installation | Code and text remain legible and pass scans; attachment remains safe |
| NFC | Multiple supported phones, different antenna positions, assembled marker wet/dry | Opens exact same URL reliably; lock and fallback QR verified |
| Accessibility | Human-readable URL transcribed; phone held from reachable path | No unsafe reach or intrusion required |

Environmental protocols and pass/fail limits must be set by the responsible materials/test engineer. Relevant formal barcode verification and weathering standards may be specified by that engineer; this document does not claim a certified ISO grade or environmental rating.

## 7. Production and handover

Family signs proof with name, dates, memorial ID, canonical URL, audience setting, site consent, mounting method, and material. Manufacturer records lot, process, operator, verification result, and an image of each finished code. Perform a 100% scan check of shipped codes and sample dimensional/material checks per supplier quality agreement.

Ship with a clean microfiber cloth, mounting/cleaning instructions approved by the installer, a paper fallback URL card, and contact details for replacements. Inspect annually and after vandalism/storm damage; clean with the supplier-approved method. Replace unreadable markers while preserving the same URL.

Request itemized quotes at 10/50/100/500 units for artwork setup, material, marking, NFC, attachment, packing, test documentation, shipping, tax, and replacements. The concept’s $8–25 estimate is not a verified quotation. No order or manufacturing payment has been placed.

## 8. Digital continuity behind the physical object

Domain renewal, TLS, database/media backups, and successor access are part of the product’s maintenance burden. Document domain ownership and recovery in the operator’s estate/business continuity plan. Test the QR after each deployment. If the service is moved, preserve the origin and `/m/<id>` route; never change the printed address simply because the software stack changes.
