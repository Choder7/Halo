# Changelog

## 1.37.1

- **Fixed**
  - A pill you dragged somewhere stays there when you click into another window and then press the shortcut to go back to Halo, instead of moving to the middle of the screen

## 1.37.0

<details>
<summary><ins>Updating from 1.36.2 or older?</ins></summary>

After dropping the new "halo.py" over the old one, right-click it, choose *Properties*, and tick **Allow executing file as program**. Then right-click it again and choose **Execute as Program** once. _(This is the last time you'll have to do this, future updates work without it.)_

</details>

- **Added**
  - Zoom the page with Ctrl + / Ctrl − / Ctrl 0 or Ctrl + mouse wheel. Halo remembers the zoom
  - Pages laid out wider than Halo, like amazon.de, are zoomed out to fit instead of being cut off on the right. Zooming such a page yourself changes only that page
  - fritz.box, printer.local and other home-network addresses open instead of being searched
- **Fixed**
  - Adding words to a Circle to Search no longer ends in Google's "400. That's an error."
  - Words that come with a picture dropped from a web page now refine its results
  - A page that crashes is reloaded once instead of leaving the panel blank
  - A pill you dragged somewhere stays there when you start typing, instead of jumping back toward where the drag began
  - Pages wider than Halo, like amazon.de, no longer show bars along the right and bottom edges with a dark square in the corner. Scrollbars are GNOME's own thin ones now
  - While you type in a text box on a web page, its keys (↑, Ctrl+Enter, Ctrl+Shift+V, …) stay with the page
  - Typing on after highlighting a suggestion searches what you typed
  - A picture dropped right after a search is no longer searched together with that search's words
  - Pictures over 20 MB are searched instead of refused
  - Several files dropped at once search the first picture among them
  - A file that isn't a picture, dropped on the search field, gets a short note instead of its location being typed in
  - Dragging a link, picture or words from Halo into some apps, such as GNOME Text Editor, no longer leaves the drag icon on screen until Halo is closed
  - File names like main.tf, Code.gs or setup.py:12 and names like System.IO are searched instead of opened as websites
  - Quick answers: log10 1000 and abs -5 get an answer, round 2.5 gives 3, and Mb vs MB converts correctly; phone numbers, dates, year ranges and a lone percentage no longer get one
  - After "Remove everything", a Halo that was still running no longer saves its old settings and searches again
  - From this version on, updating with a halo.py that isn't marked executable no longer stops the shortcut, launcher and start-at-login from opening Halo
  - Opening a second copy of halo.py no longer moves your shortcut and launcher to it (⋮ → Repair still does, only on request though)
  - First-time setup binds the default shortcut shown in the menu
  - Shortcuts of your own that run halo.py with a search are left as they are
  - Start-at-login no longer hides a Halo you had already opened
  - A settings file edited by hand with an invalid number no longer stops Halo from starting
  - "Forget after" removes older searches from disk right away
  - The pointer over Halo has the right size on portrait monitors, and keeps it after switching between screens with different scaling, such as a 4K laptop panel and an external monitor

## 1.36.2

- **Fixed**
  - Text and links dragged onto a page no longer stay stuck on screen after the drop, even once Halo is closed

## 1.36.1

- **Fixed**
  - Closing Halo now really pauses embedded videos, instead of muting them so they carry on and are heard again when Halo opens
  - Videos no longer play silently after reopening Halo

## 1.36.0

- **Added**
  - Quick answers: sums and unit conversions show their result as the first suggestion, written out properly (`2^10` → 2¹⁰ = 1024, `15% of 80`, `5 km in miles`)
  - Converts length, weight, volume, area, temperature, speed, time, data, energy, power, pressure and angles
  - Pick the answer to copy it, Tab to keep calculating with it. Enter still searches Google
  - Switch for it under ⋮ → Quick answers
- **Changed**
  - The keybind opens Halo noticeably faster
