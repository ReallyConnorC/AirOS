# Air Family and Air Kitchen

Phone website: https://reallyconnorc.github.io/AirOS/family/

## Connect your household

1. On your phone, open Air Family and sign in with the family Air OS email and password. Enter your own name for this device.
2. Create a household. This account is the owner. Under **You**, copy its family and kitchen invitation codes.
3. On the TV, sign in with that same owner account. Open **Settings → Family & Kitchen** and select the household. A single household connects automatically.
4. Other people sign in with the same family email and password and choose their own name on each device. Names are saved on the device and attached to each message or food request; changing one device does not rename another. Shared sign-in gives the same household access to every device. Separate accounts with invitation codes are also supported. A message says “Alex has said: …” on the TV. It also appears over the current app.
5. On the kitchen tablet, download **Air-Kitchen.apk**, open it and allow installation for the browser when Android asks. Open Air Kitchen. Sign in with the owner account, or create a separate account and join with the kitchen code.
6. Add real food and drinks in **Kitchen → Food & drink stock**. Enter available portions: three sandwiches means quantity 3. Edit quantities as you restock; hide things you no longer offer.
7. On a phone, choose **Menu**, add food, drinks or both and send. The kitchen displays a pop-up and speaks the name and items. Mark it ready, delivered or cancelled. A pending request reserves stock; cancellation returns it once.

The phone, TV and tablet may be on different internet connections. Traffic uses HTTPS and each person must sign in. The TV only makes outbound requests; no router forwarding is needed. Messages expire after 24 hours if not announced. Orders and stock persist online.

Keep the kitchen app open for prompt alerts. It keeps the tablet screen awake. Android notification permission enables notification banners; the in-app pop-up still works without it. The tablet uses Android's installed English text-to-speech voice. If English speech is missing, install an English voice in Android's text-to-speech settings. Phones can also open the Kitchen web page and tap **Enable announcements**.

Share the kitchen code only with people allowed to manage stock and requests. Household owners can replace invitation codes and remove members. Replacing a code does not remove existing members. Members see their own requests and messages; owners and kitchen accounts can see the household's requests.

## Maintainer setup and updates

- Run `supabase/family.sql` once in the existing project SQL editor. It creates only the `family_*` tables and functions. Row-level security and authenticated RPCs enforce household access. `supabase/family-test.sql` checks stock reservation, cancellation, idempotency, access isolation and message delivery in a transaction that is rolled back.
- GitHub Pages serves `docs/family/`. `config.js` contains only the project's public Supabase key. Never add a service-role key, Groq key or account password to these files.
- Deploy the updated `airos/` files and restart the TV service. Family selection is saved privately beside the TV's own configuration.
- Build the APK with `python tools/build-kitchen.py`. It uses the installed Android SDK 35 and JDK 21. The signing key is kept outside the repository in `~/.config/airos/android-signing/`; retain that key for future APK updates. Raise `versionCode` and `versionName` in `kitchen-android/AndroidManifest.xml` for each Android update.
- The APK bundles the website interface and connects to the same Supabase project. Rebuild it after changing the bundled page. Android 7 or newer is supported.

Messages and requests are household communication, not purchases. Nothing is ordered from a shop and no payment is taken.

## App experience in 1.1.0

Use **Home**, **TV**, **Menu**, **Kitchen**, and **You** to move between screens. Add Air Family to your phone home screen for a standalone app window: on iPhone use Safari’s Share → Add to Home Screen; on Android use Chrome’s menu → Add to home screen (or the button under You). The interface can open offline; sending messages and kitchen requests requires internet access. Keep the kitchen board open for announcements.

Existing installations: run `supabase/family-device-profiles.sql` before publishing 1.1.0 clients. This adds device identifiers and authenticated sender-name RPCs while preserving existing messages, stock and older clients. The full `family.sql` includes this migration for new installations.
