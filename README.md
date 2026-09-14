<div align="center">

# Halo

**Google, one keypress away.**

A floating search popup for Fedora Workstation.

<p>
<img alt="License GPL-3.0" src="https://img.shields.io/badge/license-GPL--3.0-4c8bf5?style=flat-square">
<img alt="Fedora Workstation 44" src="https://img.shields.io/badge/Fedora%20Workstation-44-51a2da?style=flat-square">
<img alt="GTK4 and libadwaita" src="https://img.shields.io/badge/GTK4-libadwaita-2ec27e?style=flat-square">
<img alt="One file" src="https://img.shields.io/badge/install-one%20file-f6d32d?style=flat-square">
</p>

<sub><i>✦ This passion project is heavily AI-generated.</i></sub>

</div>

<br>

This is the main Halo window you are going to see:

<div align="center">
<img src="assets/halo-window.png" alt="The Halo pill, empty, with its rim sweeping through Google's colours, then a query being typed and suggestions dropping down" width="760">
</div>

<br>

Press Enter and it unfolds into Google:

<div align="center">
<img src="assets/halo-search.png" alt="Halo expanding from the pill into a full panel of dark-mode Google results" width="760">
</div>

<br>

<br>

## What it does

- Floats above every window when activated.
- Results open **inside** it, no need to launch your browser for a quick question.
- Nice theme.
- Tells Google to deny.
- Goes away on Esc or keybind.

<br>

## Requirements

Halo is a GTK4 app, designed and tested specifically on **Fedora Workstation 44** (GNOME 50, Wayland). It might or might not work on other distributions. Nothing stops you trying, and nothing should break if it doesn't work.

Everything it needs is already preinstalled on stock Fedora Workstation:
- GTK 4, libadwaita, WebkitGTK 6
- PyGObject, Python 3

<br>

## Installation

1. Download **[halo.py](https://github.com/Choder7/Halo/raw/main/halo.py)**.
2. Move it to a location you plan on leaving it. (You can always move it to another location later, just repeat step 4 once and it will automatically repoint everything to the new location)
3. Right-click it in Files, then choose *Properties*, and tick **Allow executing file as program**.
4. Right-click it again, and choose **Execute as Program**.
5. In the popup, click **⋮** and then **Set up Halo**.

After that Halo is automatically added to your app grid, starts it at login, and binds your custom keybind(s). It shows you every file it wants to create and waits for your yes. (You can configure it what it should create before proceeding)


To update to a newer version, simply drop the newer "halo.py" over the old one. (Or delete the old one and put in the new one.) No data will be lost.

<br>

## Keybinds

The full list can be opened within Halo, under **⋮** → **Manual & shortcuts**.

Essentials:
| | |
| --- | --- |
| **Custom Keybind(s)** | Open Halo |
| **Enter** | Search |
| **Ctrl + Enter** | Open current page or search in default browser |
| **Esc/Custom Keybind(s)** | Hide |
| **Arrow up ↑** | Search history |
| **Ctrl + Shift + S** | Grab a region of the screen and search it with Lens |

You can also drop an image onto the Halo pill to Google-search it, or middle-click it to search whatever text is selected anywhere on screen.

<br>

## Where it lives

| | |
| --- | --- |
| Launcher | `~/.local/share/applications/` |
| Start at login | `~/.config/autostart/` |
| Settings | `~/.config/halo/config.json` |
| History, cookies, cache | `~/.local/share/halo/` |
| Shortcuts | ordinary GNOME custom shortcuts |

**⋯ ▸ Remove Halo's setup** deletes all of it and asks whether to keep your data. Then delete "halo.py".

<br>

## License

[GPL-3.0](LICENSE).
