import re
import itertools

import wifi.subprocess_compat as subprocess
from pbkdf2 import PBKDF2
from wifi.utils import ensure_file_exists
from wifi.exceptions import ConnectionError
import commands, sys
from time import sleep



def configuration(cell, passkey=None):
    """
    Returns a dictionary of configuration options for cell

    Asks for a password if necessary
    """
    if not cell.encrypted:
        return {
            'wireless-essid': cell.ssid,
            'wireless-channel': 'auto',
        }
    else:
        if cell.encryption_type.startswith('wpa'):
            if len(passkey) != 64:
                passkey = PBKDF2(passkey, cell.ssid, 4096).hexread(32)

            return {
                'wpa-ssid': cell.ssid,
                'wpa-psk': passkey,
                'wireless-channel': 'auto',
            }
        elif cell.encryption_type == 'wep':
            # Pass key lengths in bytes for WEP depend on type of key and key length:
            #
            #       64bit   128bit   152bit   256bit
            # hex     10      26       32       58
            # ASCII    5      13       16       29
            #
            # (source: https://en.wikipedia.org/wiki/Wired_Equivalent_Privacy)
            #
            # ASCII keys need to be prefixed with an s: in the interfaces file in order to work with linux' wireless
            # tools

            ascii_lengths = (5, 13, 16, 29)
            if len(passkey) in ascii_lengths:
                # we got an ASCII passkey here (otherwise the key length wouldn't match), we'll need to prefix that
                # with s: in our config for the wireless tools to pick it up properly
                passkey = "s:" + passkey

            return {
                'wireless-essid': cell.ssid,
                'wireless-key': passkey,
            }
        else:
            raise NotImplementedError


bound_ip_re = re.compile(r'^bound to (?P<ip_address>\S+)', flags=re.MULTILINE)


class Scheme(object):
    """
    Saved configuration for connecting to a wireless network.  This
    class provides a Python interface to the /etc/network/interfaces
    file.
    """

    interfaces = '/etc/network/interfaces'

    @classmethod
    def for_file(cls, interfaces):
        """
        A class factory for providing a nice way to specify the interfaces file
        that you want to use.  Use this instead of directly overwriting the
        interfaces Class attribute if you care about thread safety.
        """
        return type(cls)(cls.__name__, (cls,), {
            'interfaces': interfaces,
        })

    def __init__(self, interface, name, passkey = None, options=None):
        self.interface = interface
        self.name = name
        self.options = options or {}
        self.pass_ = passkey #password without encryption in case of wpa

    def __str__(self):
        """
        Returns the representation of a scheme that you would need
        in the /etc/network/interfaces file.
        """
        iface = "iface {interface}-{name} inet dhcp".format(**vars(self))
        options = ''.join("\n    {k} {v}".format(k=k, v=v) for k, v in self.options.items())
        return iface + options + '\n'

    def supplicant_str(self):
        """
        Returns the representation of a scheme that you would need
        in the /etc/wpa_supplicant/wpa_supplicant.conf file.

******************   FALTA AGREGAR QUE PASA CUANDO ES OTRO TIPO DISTINTO A WPA **********************
        """
        iface = "network={ "
        #options = '\n    ssid="{}"\n    psk="{}"'.format(self.options['wpa-ssid'],self.pass_)
        options = '\n    ssid="{}"\n    psk={}'.format(self.options['wpa-ssid'],self.options['wpa-psk'])
        return iface + options + '\n} \n'

    def __repr__(self):
        return 'Scheme(interface={interface!r}, name={name!r}, options={options!r}'.format(**vars(self))

    @classmethod
    def all(cls):
        """
        Returns an generator of saved schemes.
        """
        ensure_file_exists(cls.interfaces)
        with open(cls.interfaces, 'r') as f:
            return extract_schemes(f.read(), scheme_class=cls)

    @classmethod
    def where(cls, fn):
        return list(filter(fn, cls.all()))

    @classmethod
    def find(cls, interface, name):
        """
        Returns a :class:`Scheme` or `None` based on interface and
        name.
        """
        try:
            return cls.where(lambda s: s.interface == interface and s.name == name)[0]
        except IndexError:
            return None

    @classmethod
    def for_cell(cls, interface, name, cell, passkey=None):
        """
        Intuits the configuration needed for a specific
        :class:`Cell` and creates a :class:`Scheme` for it.
        """
        return cls(interface, name, passkey, configuration(cell, passkey))


    def save(self):
        """
        Writes the configuration to the :attr:`interfaces` file.
        """
        if self.find(self.interface, self.name):
            return
        with open(self.interfaces, 'a') as f:
            f.write('\n')
            f.write(str(self))

    def save_supplicant(self):
        """
        Writes the configuration to the :attr:`interfaces` file.
        """
        if self.find_supplicant(self.name):
            return
        file_supplicant = "/etc/wpa_supplicant/wpa_supplicant.conf"
        with open(file_supplicant, 'a') as f:
            f.write('\n')
            f.write(self.supplicant_str())

    def find_supplicant(self, ssid):
        file_supplicant = "/etc/wpa_supplicant/wpa_supplicant.conf"
        with open(file_supplicant) as f:
            lines = f.readlines()
            for i in range(len(lines)):
                if "network={" in lines[i]:
                    network = lines[i+1].split("=")[-1] #it is in the format string but with "" -> "mynetwork"
                    network = network[1:-2] #removes the ""
                    if ssid==network:
                        return True
        return False

    def delete(self):
        """
        Deletes the configuration from the :attr:`interfaces` file.
        """
        iface = "iface %s-%s inet dhcp" % (self.interface, self.name)
        content = ''
        with open(self.interfaces, 'r') as f:
            skip = False
            for line in f:
                if not line.strip():
                    skip = False
                elif line.strip() == iface:
                    skip = True
                if not skip:
                    content += line
        with open(self.interfaces, 'w') as f:
            f.write(content)

    @property
    def iface(self):
        return '{0}-{1}'.format(self.interface, self.name)

    def as_args(self):
        args = list(itertools.chain.from_iterable(
            ('-o', '{k}={v}'.format(k=k, v=v)) for k, v in self.options.items()))

        return [self.interface, "-o", "name={name}".format(name=self.name)]  + args

    def wait4ip(self):
        ip = ""
        i = 0
        time2wait = 20*2 #20 seconds
        print("Getting IP ."),
        sys.stdout.flush()
        while (i < time2wait and ip == ""):
            ip = commands.getoutput('''ifconfig wlan0 | grep "inet " | awk -F'[: ]+' '{ print $4 }' ''')
            print("."),
            sys.stdout.flush()
            sleep(0.5)
            i+=1
        if i == time2wait:
            print("Not connected")
        else:
            print(ip)
        return ip

    def activate(self):
        """
        Connects to the network as configured in this scheme.
        Returns True if can connect, otherwise returns False
        """

        subprocess.check_output(['/sbin/ifdown', self.interface], stderr=subprocess.STDOUT)
        ifup_output = subprocess.check_output(['/sbin/ifup'] + self.as_args(), stderr=subprocess.STDOUT)
        ifup_output = ifup_output.decode('utf-8')
        ip = self.wait4ip()
        if ip == "":
            return False
        else:
            return True
        #return self.parse_ifup_output(ifup_output)

    def parse_ifup_output(self, output):
        matches = bound_ip_re.search(output)
        if matches:
            return Connection(scheme=self, ip_address=matches.group('ip_address'))
        else:
            raise ConnectionError("Failed to connect to %r" % self)


class Connection(object):
    """
    The connection object returned when connecting to a Scheme.
    """
    def __init__(self, scheme, ip_address):
        self.scheme = scheme
        self.ip_address = ip_address


scheme_re = re.compile(r'iface\s+(?P<interface>[^-]+)(?:-(?P<name>\S+))?')


def extract_schemes(interfaces, scheme_class=Scheme):
    lines = interfaces.splitlines()
    while lines:
        line = lines.pop(0)

        if line.startswith('#') or not line:
            continue

        match = scheme_re.match(line)
        if match:
            options = {}
            interface, scheme = match.groups()

            if not scheme or not interface:
                continue

            while lines and lines[0].startswith(' '):
                key, value = re.sub(r'\s{2,}', ' ', lines.pop(0).strip()).split(' ', 1)
                options[key] = value

            scheme = scheme_class(interface, scheme, options)

            yield scheme
