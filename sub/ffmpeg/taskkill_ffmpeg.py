import subprocess
import re

def kill_ffmpeg_processes():
    try:
        # tasklist コマンドを実行してffmpegプロセスを検索
        result = subprocess.check_output('tasklist | findstr ffmpeg', shell=True).decode()

        # 各行からPIDを抽出
        pids = []
        for line in result.split('\n'):
            if line.strip():
                # スペースで分割し、2番目の要素（PID）を取得
                pid = line.split()[1]
                pids.append(pid)

        # 見つかった各PIDに対してtaskkillを実行
        for pid in pids:
            kill_command = f'taskkill /F /PID {pid}'
            subprocess.run(kill_command, shell=True)
            print(f'Killed ffmpeg process with PID: {pid}')

        if not pids:
            print('No ffmpeg processes found.')

    except subprocess.CalledProcessError:
        print('No ffmpeg processes found.')
    except Exception as e:
        print(f'An error occurred: {str(e)}')

if __name__ == '__main__':
    kill_ffmpeg_processes()

