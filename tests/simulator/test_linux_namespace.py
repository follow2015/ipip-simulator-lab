"""Actual namespace, low-port bind and setuid checks; run in Linux root CI."""
import asyncio
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys

import pytest

from idc_simulator.deploy import names
from idc_simulator.runtime import Runtime
from idc_simulator.tools import generate_lab

pytestmark=pytest.mark.skipif(os.getenv('IDC_LINUX_INTEGRATION')!='1',reason='requires explicit Linux root namespace integration environment')


async def diagnose(runtime,lab,device):
    ns=names(lab['name'],device['id'])['namespace']
    base=runtime.directory/lab['name']/device['id']
    parts=[f"namespace={ns} management_ip={device['management_ip']}"]
    for name in ('query.log','notification.log'):
        path=base/name
        if path.exists():
            text=path.read_text(errors='replace')[-3000:]
            parts.append(f"--- {name} ---\n{text}")
    for argv in (["ip","netns","exec",ns,"ss","-lunp"],["ip","netns","exec",ns,"ip","-br","addr"]):
        proc=await asyncio.create_subprocess_exec(*argv,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT)
        output,_=await proc.communicate()
        parts.append(f"--- {' '.join(argv)} ---\n{output.decode(errors='replace')}")
    return "\n".join(parts)


async def test_real_namespace_protocols_privilege_drop_and_cleanup(tmp_path):
    assert sys.platform=='linux' and os.geteuid()==0 and shutil.which('ip')
    account=pwd.getpwnam('nobody')
    root=Path('/tmp')/f'idc-netns-check-{os.getpid()}'
    root.mkdir(mode=0o711,exist_ok=True)
    lab=generate_lab(2,4);lab['name']=f'ci-{os.getpid()}'
    runtime=Runtime(root,mode='namespace',worker_uid=account.pw_uid)
    runtime.controller.create(lab);await runtime.start()
    resources=[names(lab['name'],dev['id']) for dev in lab['devices']]
    try:
        await runtime.supervisor.start(lab['name'])
        for device in lab['devices']:
            worker=runtime.supervisor.processes[lab['name']][device['id']]
            status=Path(f'/proc/{worker.pid}/status').read_text()
            uidline=next(line for line in status.splitlines() if line.startswith('Uid:'))
            assert all(int(uid)==account.pw_uid for uid in uidline.split()[1:])
            group_line=next(line for line in status.splitlines() if line.startswith('Groups:'))
            assert group_line.strip()=='Groups:'
            from pysnmp.hlapi.v3arch.asyncio import SnmpEngine,CommunityData,UdpTransportTarget,ContextData,ObjectType,ObjectIdentity,get_cmd
            manager=SnmpEngine()
            try:
                target=await UdpTransportTarget.create((device['management_ip'],161),timeout=2,retries=0)
                result=await get_cmd(manager,CommunityData('sim-read'),target,ContextData(),ObjectType(ObjectIdentity('1.3.6.1.2.1.1.5.0')))
                assert result[0] is None and not result[1], await diagnose(runtime,lab,device)
            finally:manager.close_dispatcher()
            import asyncssh
            async with asyncssh.connect(device['management_ip'],port=22,username='sim',password='sim-password',known_hosts=None,config=None) as connection:
                assert (await connection.run('show version')).exit_status==0
            channel=runtime.channels[(device['id'],'query')]
            script="""import json,socket,sys
sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
sock.settimeout(5);sock.connect(sys.argv[1])
sock.sendall((json.dumps({"method":"snapshot","device":sys.argv[2],"role":"query"})+"\\n").encode())
data=b""
while b"\\n" not in data:
 piece=sock.recv(65536)
 if not piece: break
 data+=piece
reply=json.loads(data)
assert reply.get("ok") is False, reply
"""
            probe=await asyncio.create_subprocess_exec('ip','netns','exec',names(lab['name'],device['id'])['namespace'],
                'setpriv','--reuid',str(account.pw_uid),'--regid',str(account.pw_gid),'--clear-groups',sys.executable,'-c',script,str(channel.path),device['id'])
            assert await probe.wait()==0
        dev=lab['devices'][0]['id'];old=runtime.supervisor.processes[lab['name']][dev]
        old.kill();await old.wait()
        for _ in range(200):
            current=runtime.supervisor.processes[lab['name']][dev]
            if current.pid!=old.pid and (root/lab['name']/dev/'ready.json').exists():break
            await asyncio.sleep(.1)
        assert current.pid!=old.pid
    finally:
        await runtime.close()
    namespaces=subprocess.check_output(['ip','netns','list'],text=True)
    assert all(resource['namespace'] not in namespaces for resource in resources)
    assert subprocess.run(['ip','link','show',resources[0]['bridge']],capture_output=True).returncode!=0
    shutil.rmtree(root)
