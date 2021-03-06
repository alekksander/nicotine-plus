# COPYRIGHT (C) 2020 Nicotine+ Team
# COPYRIGHT (C) 2016-2017 Michael Labouebe <gfarmerfr@free.fr>
# COPYRIGHT (C) 2008-2009 Quinox <quinox@users.sf.net>
# COPYRIGHT (C) 2006-2009 Daelstorm <daelstorm@gmail.com>
#
# GNU GENERAL PUBLIC LICENSE
#    Version 3, 29 June 2007
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from gi.repository import GObject
from gi.repository import Gtk


def combo_box_dialog(parent, title, message, default_text="",
                     option=False, optionmessage="",
                     optionvalue=False, droplist=[]):

    self = Gtk.MessageDialog(
        transient_for=parent,
        flags=0,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.OK_CANCEL,
        text=title
    )
    self.set_default_size(500, -1)
    self.set_modal(True)
    self.format_secondary_text(message)

    self.gotoption = option

    self.combo_list = Gtk.ListStore(GObject.TYPE_STRING)
    self.combo = Gtk.ComboBox.new_with_model_and_entry(model=self.combo_list)
    self.combo.set_entry_text_column(0)

    for i in droplist:
        self.combo_list.append([i])

    self.combo.get_child().set_text(default_text)

    self.get_message_area().pack_start(self.combo, False, False, 0)

    self.combo.show()
    self.combo.grab_focus()

    if self.gotoption:

        self.option = Gtk.CheckButton()
        self.option.set_active(optionvalue)
        self.option.set_label(optionmessage)
        self.option.show()

        self.get_message_area().pack_start(self.option, False, False, 0)

    result = None
    if self.run() == Gtk.ResponseType.OK:
        if self.gotoption:
            result = [self.combo.get_child().get_text(), self.option.get_active()]
        else:
            result = self.combo.get_child().get_text()

    self.destroy()

    return result


def entry_dialog(parent, title, message, default=""):

    self = Gtk.MessageDialog(
        transient_for=parent,
        flags=0,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.OK_CANCEL,
        text=title
    )
    self.set_default_size(500, -1)
    self.set_modal(True)
    self.format_secondary_text(message)

    entry = Gtk.Entry()
    entry.set_activates_default(True)
    entry.set_text(default)
    self.get_message_area().pack_start(entry, True, True, 0)
    entry.show()

    result = None
    if self.run() == Gtk.ResponseType.OK:
        result = entry.get_text()

    self.destroy()

    return result


def option_dialog(parent, title, message, callback, callback_data=None, checkbox_label="", third=""):

    self = Gtk.MessageDialog(
        transient_for=parent,
        flags=0,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.OK_CANCEL,
        text=title
    )
    self.connect("response", callback, callback_data)
    self.set_modal(True)
    self.format_secondary_text(message)

    if checkbox_label:
        self.checkbox = Gtk.CheckButton()
        self.checkbox.set_label(checkbox_label)
        self.get_message_area().pack_start(self.checkbox, False, False, 0)
        self.checkbox.show()

    if third:
        self.add_button(third, Gtk.ResponseType.REJECT)

    self.show()
