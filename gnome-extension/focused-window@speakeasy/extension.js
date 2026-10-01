import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

// Wayland gives other programs no way to see the focused window, and GNOME
// disabled org.gnome.Shell.Eval, so the shell itself has to answer.
const IFACE_XML = `
<node>
  <interface name="org.gnome.Shell.FocusedWindow">
    <method name="Get">
      <arg type="s" name="json" direction="out"/>
    </method>
  </interface>
</node>`;

export default class FocusedWindowExtension extends Extension {
    enable() {
        this._dbusId = Gio.DBus.session.register_object(
            '/org/gnome/Shell/FocusedWindow',
            Gio.DBusNodeInfo.new_for_xml(IFACE_XML).interfaces[0],
            (connection, sender, path, iface, method, params, invocation) => {
                if (method === 'Get')
                    invocation.return_value(new GLib.Variant('(s)', [this._focused()]));
            },
            null,
            null
        );
    }

    disable() {
        if (this._dbusId) {
            Gio.DBus.session.unregister_object(this._dbusId);
            this._dbusId = null;
        }
    }

    _focused() {
        const win = global.display.focus_window;
        if (!win)
            return JSON.stringify({});
        const app = Shell.WindowTracker.get_default().get_window_app(win);
        return JSON.stringify({
            wm_class: win.get_wm_class() ?? '',
            title: win.get_title() ?? '',
            app_id: app?.get_id() ?? '',
        });
    }
}
