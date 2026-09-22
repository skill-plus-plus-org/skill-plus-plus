# Retro: Skycast 1.0, six weeks after launch

Team: three people, one Android and iOS app that shows a 7-day forecast.

- We shipped a beta to 40 users two weeks before launch. They found 11 bugs,
  and 9 of them were fixed before 1.0.
- The crash rate went from 4% of sessions in the beta to 0.5% after launch,
  mostly by removing a background refresh that ran on low memory.
- App start time dropped from 3.2 seconds to 1.1 seconds after we cached the
  last forecast on the device instead of waiting for the network.
- We cut hourly radar maps from 1.0. They took a third of the design time in
  the first month and only 2 of the 40 beta users opened them.
- Store rating after six weeks: 4.6 from 310 ratings. The most common
  complaint is that there is no widget.
- What we would do again: a weekly 20-minute demo to the whole team, which
  caught two misunderstandings about the forecast units early.
