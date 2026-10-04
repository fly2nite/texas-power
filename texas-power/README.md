# True Cost: every Texas electricity plan, priced on your real usage

- `index.html` is the website. Users upload their Smart Meter Texas file; nothing is sent anywhere.
- `data/plans.json` is the plan list the website reads. It's rebuilt every morning.
- `crawler/` finds and reads plans: Power to Choose, provider websites, and nearby facts-label addresses.
- `crawler/providers.json` is the list of provider websites to search. Add new providers there.
- `data/crawl_report.json` shows what each daily run found and what failed.
- Referral links: edit the `REFERRAL_LINKS` list near the top of the script in `index.html`.
