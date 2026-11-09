import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'dsh-novel-proofread'
export const version = '1.0.0'

// Declare services this plugin needs from the host.
// DSH starts the injected scope only when these services are active,
// and tears it down with them — so the plugin works in every session.
export const inject = ['tools', 'llm']

/**
 * Build the novel-proofread tool definition.
 * The model invokes it to run Chinese web-novel txt proofreading locally.
 */
function buildProofreadTool(skillDir) {
  const mainScript = join(skillDir, 'proofread.py')

  return {
    id: 'novel-proofread',
    name: '小说校对',
    description:
      '中文网络小说 txt 一站式校对技能：广告/屏蔽字/乱码/章节结构/重复段落/标点/错别字三维度校对，纯本地运行，不上传任何数据。\n' +
      '可用子命令：prepare / init / scan / ctx / judge / apply / pending / confirm / resolve / renumber / diff / finalize。\n' +
      '使用前请先运行 prepare 检查文件，再 init 生成批次码和备份，scan 产出候选清单，' +
      '随后在对话中逐条判定，最后 apply 修改。',
    inputSchema: {
      type: 'object',
      properties: {
        command: {
          type: 'string',
          enum: [
            'prepare', 'init', 'scan', 'ctx', 'judge', 'apply',
            'pending', 'confirm', 'resolve', 'renumber', 'diff', 'finalize'
          ],
          description: '要执行的校对子命令'
        },
        file: {
          type: 'string',
          description: '要校对的 txt 文件路径（prepare/init/scan/apply/diff/finalize 需要）'
        },
        meta: {
          type: 'string',
          description: '批次信息 JSON 路径（scan/judge/apply/pending/confirm/resolve/renumber/diff/finalize 需要）'
        },
        args: {
          type: 'string',
          description: '透传给 proofread.py 的额外参数字符串'
        }
      },
      required: ['command']
    },
    async execute(args, exec) {
      const { command, file, meta, args: extraArgs } = args
      const python = 'python3'
      const cmd = [python, mainScript, command]

      if (file) cmd.push('--file', file)
      if (meta) cmd.push('--meta', meta)
      if (extraArgs) cmd.push(...extraArgs.split(/\s+/).filter(Boolean))

      const { spawn } = await import('node:child_process')
      return new Promise((resolve, reject) => {
        const child = spawn(python, cmd, { encoding: 'utf8' })
        let stdout = ''
        let stderr = ''
        child.stdout.on('data', (d) => { stdout += d.toString() })
        child.stderr.on('data', (d) => { stderr += d.toString() })
        child.on('close', (code) => {
          if (code === 0) {
            resolve({ ok: true, output: stdout.trim(), stderr: stderr.trim() })
          } else {
            reject(new Error(`proofread.py ${command} exited ${code}: ${stderr.trim() || stdout.trim()}`))
          }
        })
        exec.signal.addEventListener('abort', () => child.kill(), { once: true })
      })
    },
    presentCall: (args) => ({
      card: 'generic',
      title: '小说校对：' + (args?.command || ''),
      kind: 'tool',
      rawInput: args
    })
  }
}

export function apply(ctx, config = {}) {
  const __dirname = fileURLToPath(new URL('.', import.meta.url))
  const skillDir = join(__dirname, 'scripts')

  // Expose skill directory on ctx for direct access
  Object.defineProperty(ctx, 'novelProofreadSkillDir', {
    value: skillDir,
    enumerable: true,
    configurable: false
  })

  // Register the proofread tool so it reaches the model on every request
  if (typeof ctx.tools?.register === 'function') {
    try {
      ctx.tools.register(buildProofreadTool(skillDir))
    } catch (error) {
      console.error(`[dsh-novel-proofread] registration skipped: ${error}`)
    }
  }
}
