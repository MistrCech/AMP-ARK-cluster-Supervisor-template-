"""Windows API pres ctypes - jen to, co supervisor na Windows potrebuje.

Ciste standardni knihovna (ctypes), zadny psutil ani pywin32. Na Linuxu se
tenhle modul neimportuje.

Omezeni: pocita s jednou skupinou procesoru (do 64 logickych CPU). EPYC 7551P
ma 64 logickych, takze se vejde; nad 64 by Windows rozdelil CPU do skupin a
afinita by platila jen pro prvni z nich.
"""
import ctypes
from ctypes import wintypes

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_psapi = ctypes.WinDLL("psapi", use_last_error=True)

RELATION_PROCESSOR_CORE = 0
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_AFFINITY = 0x00000010
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class _ProcessorCore(ctypes.Structure):
    _fields_ = [("Flags", ctypes.c_ubyte)]


class _NumaNode(ctypes.Structure):
    _fields_ = [("NodeNumber", wintypes.DWORD)]


class _CacheDescriptor(ctypes.Structure):
    _fields_ = [("Level", ctypes.c_ubyte), ("Associativity", ctypes.c_ubyte),
                ("LineSize", wintypes.WORD), ("Size", wintypes.DWORD),
                ("Type", ctypes.c_int)]


class _Info(ctypes.Union):
    _fields_ = [("ProcessorCore", _ProcessorCore), ("NumaNode", _NumaNode),
                ("Cache", _CacheDescriptor), ("Reserved", ctypes.c_ulonglong * 2)]


class _SystemLogicalProcessorInformation(ctypes.Structure):
    _fields_ = [("ProcessorMask", ctypes.c_size_t), ("Relationship", ctypes.c_int),
                ("Info", _Info)]


class _ProcessMemoryCounters(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t)]


class _JobBasicLimitInformation(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _JobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JobBasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


_k32.GetLogicalProcessorInformation.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
_k32.GetLogicalProcessorInformation.restype = wintypes.BOOL
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.GetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_size_t),
                                        ctypes.POINTER(ctypes.c_size_t)]
_k32.GetProcessAffinityMask.restype = wintypes.BOOL
_k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                         wintypes.DWORD]
_k32.SetInformationJobObject.restype = wintypes.BOOL
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.AssignProcessToJobObject.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
_k32.GetProcessTimes.restype = wintypes.BOOL
_psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessMemoryCounters),
                                        wintypes.DWORD]
_psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def _cpus(mask):
    return {cpu for cpu in range(ctypes.sizeof(ctypes.c_size_t) * 8) if mask >> cpu & 1}


def physical_cores():
    """Fyzicka jadra jako mnoziny logickych CPU (vcetne SMT sourozence)."""
    size = wintypes.DWORD(0)
    _k32.GetLogicalProcessorInformation(None, ctypes.byref(size))
    count = size.value // ctypes.sizeof(_SystemLogicalProcessorInformation)
    if not count:
        return []
    buf = (_SystemLogicalProcessorInformation * count)()
    if not _k32.GetLogicalProcessorInformation(buf, ctypes.byref(size)):
        return []
    return [_cpus(item.ProcessorMask) for item in buf
            if item.Relationship == RELATION_PROCESSOR_CORE]


def allowed_cpus():
    """CPU, ktere smi pouzit tenhle proces (AMP muze instanci omezit)."""
    proc_mask, sys_mask = ctypes.c_size_t(0), ctypes.c_size_t(0)
    if not _k32.GetProcessAffinityMask(_k32.GetCurrentProcess(),
                                       ctypes.byref(proc_mask), ctypes.byref(sys_mask)):
        raise OSError(ctypes.get_last_error(), "GetProcessAffinityMask")
    return _cpus(proc_mask.value)


def bind_to_job(handle, cpus=None):
    """Vlozi proces do noveho job objectu a vrati handle jobu.

    Volajici handle DRZI, dokud proces bezi: job ma KILL_ON_JOB_CLOSE, takze
    kdyz supervisor skonci jakkoli (i zabity z AMP), Windows zavrou jeho
    handly a ukonci i mapy. Jinak by bezely dal jako sirotci a pristi start
    by pustil druhou kopii nad stejnym savem - ASA pri obsazenem portu tise
    vezme jiny (overeno), takze by si toho nikdo nevsiml.

    cpus: limit afinity. SetProcessAffinityMask nestaci: ASA si afinitu pri
    startu prepise zpet na vsechna CPU (overeno v94.15 - ProcessorAffinity -1
    hned po nabehnuti). Limit jobu proces rozsirit nemuze a plati i pro
    vlakna, ktera uz bezi.
    """
    flags, mask = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0
    if cpus:
        flags |= JOB_OBJECT_LIMIT_AFFINITY
        for cpu in cpus:
            mask |= 1 << cpu
    job = _k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError(ctypes.get_last_error(), "CreateJobObjectW")
    try:
        info = _JobExtendedLimitInformation()
        info.BasicLimitInformation.LimitFlags = flags
        info.BasicLimitInformation.Affinity = mask
        if not _k32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject")
        if not _k32.AssignProcessToJobObject(job, int(handle)):
            raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject")
    except BaseException:
        _k32.CloseHandle(job)
        raise
    return job


def close_handle(handle):
    """Zavre handle (napr. jobu). U jobu s KILL_ON_JOB_CLOSE ukonci, co v nem bezi."""
    if handle:
        _k32.CloseHandle(handle)


def process_stats(handle):
    """(RAM v kB = working set, CPU v sekundach od startu) nebo (0, None)."""
    handle = int(handle)
    counters = _ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    rss_kb = (counters.WorkingSetSize // 1024
              if _psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
              else 0)
    times = [wintypes.FILETIME() for _ in range(4)]
    if not _k32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return rss_kb, None
    kernel, user = times[2], times[3]
    hundred_ns = ((kernel.dwHighDateTime << 32 | kernel.dwLowDateTime)
                  + (user.dwHighDateTime << 32 | user.dwLowDateTime))
    return rss_kb, hundred_ns / 1e7
