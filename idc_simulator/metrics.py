from collections import Counter, defaultdict, deque
import json
import math
import re

try:
    import resource
except ImportError:
    resource = None


_SAMPLE = re.compile(r'(?P<name>idc_[a-zA-Z_][a-zA-Z0-9_]*)(?:\{(?P<labels>.*)\})? (?P<value>[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?|[+-]?Inf|NaN)')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\\n]|\\[\\"n])*)"(?:,|$)')


def worker_sample(line, device, role):
    if len(line) > 8192:
        return None
    match = _SAMPLE.fullmatch(line)
    if not match:
        return None
    labels = match['labels'] or ''
    offset = 0
    seen = {'device', 'role'}
    while offset < len(labels):
        label = _LABEL.match(labels, offset)
        if not label or label[1] in seen:
            return None
        seen.add(label[1])
        offset = label.end()
    if labels.endswith(','):
        return None
    import json
    provenance = f'device={json.dumps(device)},role={json.dumps(role)}'
    return f"{match['name']}{{{provenance}{','+labels if labels else ''}}} {match['value']}\n"


class Metrics:
    def __init__(self):
        self.counts = Counter()
        self.samples = defaultdict(lambda: deque(maxlen=2048))
        self.gauges = {}

    def increment(self, name, value=1, **labels):
        key = (name, tuple(sorted(labels.items()))) if labels else name
        self.counts[key] += value

    def observe(self, name, value):
        self.samples[name].append(value)

    @staticmethod
    def _line(key, value):
        if isinstance(key, str):
            return f"idc_{key} {value}"
        name, labels = key
        rendered = ",".join(f"{label}={json.dumps(str(text))}" for label, text in labels)
        return f"idc_{name}{{{rendered}}} {value}"

    def render(self):
        merged = {**self.counts, **self.gauges}
        ordered = sorted(merged, key=lambda key: self._line(key, merged[key]))
        lines = [self._line(key, merged[key]) for key in ordered]
        for k, values in sorted(self.samples.items()):
            data = sorted(values)
            for q in (.5, .95, .99):
                lines.append(f'idc_{k}{{quantile="{q}"}} {data[max(0, math.ceil(q*len(data))-1)]}')
        usage = resource.getrusage(resource.RUSAGE_SELF) if resource is not None else None
        if usage is not None:
            lines.extend((f"idc_process_cpu_seconds {usage.ru_utime+usage.ru_stime}", f"idc_process_maxrss_native {usage.ru_maxrss}"))
        return "\n".join(lines)+"\n"
