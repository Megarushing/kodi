/*
 *  Copyright (C) 2013-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "ScriptInvocationManager.h"

#include "interfaces/generic/ILanguageInvocationHandler.h"
#include "interfaces/generic/ILanguageInvoker.h"
#include "interfaces/generic/LanguageInvokerThread.h"
#include "ServiceBroker.h"
#include "messaging/ApplicationMessenger.h"
#include "threads/SystemClock.h"
#include "utils/FileUtils.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/XTimeUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <cerrno>
#include <memory>
#include <mutex>
#include <tuple>
#include <utility>
#include <vector>

namespace
{
// LOCAL PATCH (megarushing): warm plugin invoker pool (add-ons with reuselanguageinvoker=true).
// Each plugin row otherwise starts a new Python interpreter and re-imports its modules (~1-1.5 s
// of CPU on a Shield), all under one shared GIL.
// Max invokers (busy + idle) per plugin script. A row that finds all of them busy waits for one.
constexpr size_t INVOKER_POOL_MAX_PER_SCRIPT = 4;
// How long a row waits for a busy invoker to become idle before it starts a new (cold) one.
constexpr auto INVOKER_POOL_WAIT = std::chrono::milliseconds(4000);
// An idle invoker unused this long is stopped (frees memory, drops stale module state).
constexpr auto INVOKER_POOL_IDLE_EXPIRY = std::chrono::seconds(300);
// A claimed invoker that was not executed within this time becomes claimable again.
constexpr auto INVOKER_POOL_CLAIM_TIMEOUT = std::chrono::seconds(30);
} // namespace

namespace
{
// how long Uninitialize() waits for scripts it asked to stop (upstream xbmc/xbmc#29954, all platforms)
constexpr auto SCRIPT_STOP_TIMEOUT = std::chrono::seconds(5);
// and for those that stop() already gave 5 s and SystemExit
constexpr auto SCRIPT_STOP_GRACE = std::chrono::seconds(1);
} // namespace

CScriptInvocationManager::~CScriptInvocationManager()
{
  Uninitialize();
}

CScriptInvocationManager& CScriptInvocationManager::GetInstance()
{
  static CScriptInvocationManager s_instance;
  return s_instance;
}

void CScriptInvocationManager::Process()
{
  std::unique_lock lock(m_critSection);
  // go through all active threads and find and remove all which are done
  std::erase_if(m_scripts,
                [&paths = m_scriptPaths](const auto& it)
                {
                  const auto& [key, script] = it;
                  if (script.done)
                    paths.erase(script.script);
                  return script.done;
                });

  PrunePoolLocked();

  // we can leave the lock now
  lock.unlock();

  // let the invocation handlers do their processing
  std::ranges::for_each(m_invocationHandlers, [](auto& handler) { handler.second->Process(); });
}

void CScriptInvocationManager::Uninitialize()
{
  std::unique_lock lock(m_critSection);

  // execute Process() once more to handle the remaining scripts
  Process();

  // it is safe to release early, thread must be in m_scripts too
  m_lastInvokerThread = nullptr;
  m_invokerPool.clear();

  // make sure all scripts are done
  std::vector<LanguageInvokerThread> tempList;
  std::ranges::transform(m_scripts, std::back_inserter(tempList),
                         [](const auto& script) { return script.second; });

  m_scripts.clear();
  m_scriptPaths.clear();

  // we can leave the lock now
  lock.unlock();

  // finally stop and remove the finished threads but we do it outside of any
  // locks in case of any callbacks from the stop or destruction logic of
  // CLanguageInvokerThread or the ILanguageInvoker implementation
  std::ranges::for_each(tempList,
                        [](auto& val)
                        {
                          if (!val.done)
                            val.thread->Stop(false);
                        });

  // a script that catches SystemExit or blocks in a call that never returns would keep
  // Kodi from exiting, so don't wait for it forever
  XbmcThreads::EndTime<> timeout(SCRIPT_STOP_TIMEOUT);
  XbmcThreads::EndTime<> grace(SCRIPT_STOP_GRACE);
  std::ranges::for_each(tempList,
                        [&timeout, &grace](auto& val)
                        {
                          const auto& end =
                              val.thread->GetState() == InvokerStateFailed ? grace : timeout;
                          if (val.done || val.thread->WaitForExit(end.GetTimeLeft()))
                            return;
                          CLog::Log(LOGERROR,
                                    "CScriptInvocationManager: script {} did not stop, not "
                                    "waiting for it",
                                    val.script);
                          // never released: destroying the thread would join it
                          new std::shared_ptr<CLanguageInvokerThread>(val.thread);
                        });

  lock.lock();

  tempList.clear();

  // uninitialize all invocation handlers and then remove them
  std::ranges::for_each(m_invocationHandlers,
                        [](auto& handler) { handler.second->Uninitialize(); });

  m_invocationHandlers.clear();
}

#ifdef TARGET_WEBOS
std::chrono::milliseconds CScriptInvocationManager::GetShutdownScriptTimeout(
    std::chrono::milliseconds fallback) const
{
  std::unique_lock lock(m_critSection);
  if (!m_shutdownDeadline)
    return fallback;
  const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(
      *m_shutdownDeadline - std::chrono::steady_clock::now());
  return std::min(fallback, std::max(std::chrono::milliseconds::zero(), remaining));
}
#endif

void CScriptInvocationManager::RegisterLanguageInvocationHandler(
    ILanguageInvocationHandler* invocationHandler, const std::string& extension)
{
  if (invocationHandler == nullptr || extension.empty())
    return;

  std::string ext = extension;
  StringUtils::ToLower(ext);
  if (!StringUtils::StartsWithNoCase(ext, "."))
    ext = "." + ext;

  std::unique_lock lock(m_critSection);

  const bool known =
      std::ranges::any_of(m_invocationHandlers, [invocationHandler](const auto& handler)
                          { return handler.second == invocationHandler; });

  bool inserted;
  std::tie(std::ignore, inserted) = m_invocationHandlers.emplace(ext, invocationHandler);

  // automatically initialize the invocation handler if it's a new one
  if (inserted && !known)
    invocationHandler->Initialize();
}

void CScriptInvocationManager::RegisterLanguageInvocationHandler(
    ILanguageInvocationHandler* invocationHandler, const std::set<std::string>& extensions)
{
  if (invocationHandler == nullptr || extensions.empty())
    return;

  std::ranges::for_each(extensions, [&invocationHandler, this](const auto& extension)
                        { RegisterLanguageInvocationHandler(invocationHandler, extension); });
}

void CScriptInvocationManager::UnregisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler)
{
  if (invocationHandler == nullptr)
    return;

  std::unique_lock lock(m_critSection);
  //  get all extensions of the given language invoker
  std::erase_if(m_invocationHandlers, [&invocationHandler](const auto& handler)
                { return handler.second == invocationHandler; });

  // automatically uninitialize the invocation handler
  invocationHandler->Uninitialize();
}

bool CScriptInvocationManager::HasLanguageInvoker(const std::string &script) const
{
  std::string extension = URIUtils::GetExtension(script);
  StringUtils::ToLower(extension);

  std::unique_lock lock(m_critSection);
  const auto it = m_invocationHandlers.find(extension);
  return it != m_invocationHandlers.end() && it->second != nullptr;
}

bool CScriptInvocationManager::IsPooledThreadDoneLocked(const PooledInvoker& entry) const
{
  const auto it = m_scripts.find(entry.thread->GetId());
  return it == m_scripts.end() || it->second.done;
}

void CScriptInvocationManager::PrunePoolLocked()
{
  const auto now = std::chrono::steady_clock::now();
  std::erase_if(m_invokerPool,
                [this, now](PooledInvoker& entry)
                {
                  if (IsPooledThreadDoneLocked(entry))
                    return true;
                  // a claim that never reached ExecutePooled() must not hold its slot forever
                  if (entry.claimed && now - entry.lastUsed > INVOKER_POOL_CLAIM_TIMEOUT)
                    entry.claimed = false;
                  if (!entry.claimed && entry.thread->Reuseable(entry.script) &&
                      now - entry.lastUsed > INVOKER_POOL_IDLE_EXPIRY)
                  {
                    CLog::Log(LOGINFO, "InvokerPool: expire id {} handle {} {}",
                              entry.thread->GetId(), entry.pluginHandle, entry.script);
                    entry.thread->Release();
                    return true;
                  }
                  return false;
                });
}

int CScriptInvocationManager::GetReusablePluginHandle(const std::string& script)
{
  // Never block the GUI thread: it would freeze rendering while it waits.
  const auto messenger = CServiceBroker::GetAppMessenger();
  const bool mayWait = messenger && !messenger->IsProcessThread();
  const auto start = std::chrono::steady_clock::now();
  bool waited = false;
  while (true)
  {
    {
      std::unique_lock lock(m_critSection);
      PrunePoolLocked();

      size_t live = 0;
      for (auto& entry : m_invokerPool)
      {
        if (entry.script != script)
          continue;
        ++live;
        if (!entry.claimed && entry.thread->Reuseable(script))
        {
          entry.claimed = true;
          entry.lastUsed = std::chrono::steady_clock::now();
          CLog::Log(LOGINFO, "InvokerPool: reuse id {} handle {} waited {} ms {}",
                    entry.thread->GetId(), entry.pluginHandle,
                    std::chrono::duration_cast<std::chrono::milliseconds>(entry.lastUsed - start)
                        .count(),
                    script);
          return entry.pluginHandle;
        }
      }
      // room for one more invoker of this script (or waiting is not allowed / took too long):
      // start a new one
      if (live < INVOKER_POOL_MAX_PER_SCRIPT || !mayWait ||
          std::chrono::steady_clock::now() - start >= INVOKER_POOL_WAIT)
      {
        if (waited)
          CLog::Log(LOGINFO, "InvokerPool: no idle invoker after {} ms (live {}), cold start {}",
                    std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now() - start)
                        .count(),
                    live, script);
        return -1;
      }
    }
    waited = true;
    KODI::TIME::Sleep(std::chrono::milliseconds(10));
  }
}

std::shared_ptr<ILanguageInvoker> CScriptInvocationManager::GetLanguageInvoker(
    const std::string& script)
{
  std::unique_lock lock(m_critSection);

  if (m_lastInvokerThread)
  {
    if (m_lastInvokerThread->Reuseable(script))
    {
      CLog::Log(LOGDEBUG, "{} - Reusing LanguageInvokerThread {} for script {}", __FUNCTION__,
                m_lastInvokerThread->GetId(), script);
      // LOCAL PATCH (megarushing): no Reset() here. CLanguageInvokerThread::execute() resets the
      // state under its mutex, after Process() has checked the result of the previous run.
      return m_lastInvokerThread->GetInvoker();
    }
    m_lastInvokerThread->Release();
    m_lastInvokerThread = nullptr;
  }

  std::string extension = URIUtils::GetExtension(script);
  StringUtils::ToLower(extension);

  const auto it = m_invocationHandlers.find(extension);
  if (it != m_invocationHandlers.end() && it->second != nullptr)
    return it->second->CreateInvoker();

  return {};
}

int CScriptInvocationManager::ExecuteAsync(
    const std::string& script,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    bool reuseable /* = false */,
    int pluginHandle /* = -1 */)
{
  if (script.empty())
    return -1;

  if (!CFileUtils::Exists(script, false))
  {
    CLog::Log(LOGERROR, "{} - Not executing non-existing script {}", __FUNCTION__, script);
    return -1;
  }

  if (reuseable && pluginHandle >= 0)
    return ExecutePooled(script, addon, arguments, pluginHandle);

  auto invoker = GetLanguageInvoker(script);
  return ExecuteAsync(script, invoker, addon, arguments, reuseable, pluginHandle);
}

int CScriptInvocationManager::ExecutePooled(const std::string& script,
                                            const ADDON::AddonPtr& addon,
                                            const std::vector<std::string>& arguments,
                                            int pluginHandle)
{
  std::unique_lock lock(m_critSection);
  std::shared_ptr<CLanguageInvokerThread> invokerThread;
  for (auto& entry : m_invokerPool)
  {
    if (entry.claimed && entry.pluginHandle == pluginHandle && entry.script == script)
    {
      invokerThread = entry.thread;
      entry.claimed = false; // execute() below resets its state, so it is not Reuseable() again
      entry.lastUsed = std::chrono::steady_clock::now();
      break;
    }
  }

  if (invokerThread)
  {
    if (addon != nullptr)
      invokerThread->SetAddon(addon);
    // execute() takes the thread's mutex, so it runs once the thread waits for its next job
    invokerThread->Execute(script, arguments);
    return invokerThread->GetId();
  }

  std::string extension = URIUtils::GetExtension(script);
  StringUtils::ToLower(extension);
  const auto handler = m_invocationHandlers.find(extension);
  if (handler == m_invocationHandlers.end() || handler->second == nullptr)
    return -1;
  auto invoker = handler->second->CreateInvoker();
  if (invoker == nullptr)
    return -1;

  invokerThread = std::make_shared<CLanguageInvokerThread>(invoker, this, true);
  if (addon != nullptr)
    invokerThread->SetAddon(addon);
  invokerThread->SetId(m_nextId++);

  LanguageInvokerThread thread = {invokerThread, script, false, true};
  m_scripts.insert(std::make_pair(invokerThread->GetId(), thread));
  m_scriptPaths.insert(std::make_pair(script, invokerThread->GetId()));
  m_invokerPool.push_back(
      {invokerThread, script, pluginHandle, false, std::chrono::steady_clock::now()});
  CLog::Log(LOGINFO, "InvokerPool: new id {} handle {} (pool {}) {}", invokerThread->GetId(),
            pluginHandle, m_invokerPool.size(), script);

  invokerThread->Execute(script, arguments);
  return invokerThread->GetId();
}

int CScriptInvocationManager::ExecuteAsync(
    const std::string& script,
    const std::shared_ptr<ILanguageInvoker>& languageInvoker,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    bool reuseable /* = false */,
    int pluginHandle /* = -1 */)
{
  if (script.empty() || languageInvoker == nullptr)
    return -1;

  if (!CFileUtils::Exists(script, false))
  {
    CLog::Log(LOGERROR, "{} - Not executing non-existing script {}", __FUNCTION__, script);
    return -1;
  }

  std::unique_lock lock(m_critSection);

  if (m_lastInvokerThread && m_lastInvokerThread->GetInvoker() == languageInvoker)
  {
    if (addon != nullptr)
      m_lastInvokerThread->SetAddon(addon);

    // After we leave the lock, m_lastInvokerThread can be released -> copy!
    auto invokerThread = m_lastInvokerThread;
    lock.unlock();
    invokerThread->Execute(script, arguments);

    return invokerThread->GetId();
  }

  m_lastInvokerThread = std::make_shared<CLanguageInvokerThread>(languageInvoker, this, reuseable);
  if (m_lastInvokerThread == nullptr)
    return -1;

  if (addon != nullptr)
    m_lastInvokerThread->SetAddon(addon);

  m_lastInvokerThread->SetId(m_nextId++);
  m_lastPluginHandle = pluginHandle;

  LanguageInvokerThread thread = {m_lastInvokerThread, script, false};
  m_scripts.insert(std::make_pair(m_lastInvokerThread->GetId(), thread));
  m_scriptPaths.insert(std::make_pair(script, m_lastInvokerThread->GetId()));
  // After we leave the lock, m_lastInvokerThread can be released -> copy!
  auto invokerThread = m_lastInvokerThread;
  lock.unlock();
  invokerThread->Execute(script, arguments);

  return invokerThread->GetId();
}

int CScriptInvocationManager::ExecuteSync(
    const std::string& script,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    uint32_t timeoutMs /* = 0 */,
    bool waitShutdown /* = false */)
{
  if (script.empty())
    return -1;

  if (!CFileUtils::Exists(script, false))
  {
    CLog::Log(LOGERROR, "{} - Not executing non-existing script {}", __FUNCTION__, script);
    return -1;
  }

  auto invoker = GetLanguageInvoker(script);
  return ExecuteSync(script, invoker, addon, arguments, timeoutMs, waitShutdown);
}

int CScriptInvocationManager::ExecuteSync(
    const std::string& script,
    const std::shared_ptr<ILanguageInvoker>& languageInvoker,
    const ADDON::AddonPtr& addon /* = ADDON::AddonPtr() */,
    const std::vector<std::string>& arguments /* = std::vector<std::string>() */,
    uint32_t timeoutMs /* = 0 */,
    bool waitShutdown /* = false */)
{
  int scriptId = ExecuteAsync(script, languageInvoker, addon, arguments);
  if (scriptId < 0)
    return -1;

  bool timeout = timeoutMs > 0;
  while ((!timeout || timeoutMs > 0) && IsRunning(scriptId))
  {
    unsigned int sleepMs = 100U;
    if (timeout && timeoutMs < sleepMs)
      sleepMs = timeoutMs;

    KODI::TIME::Sleep(std::chrono::milliseconds(sleepMs));

    if (timeout)
      timeoutMs -= sleepMs;
  }

  if (IsRunning(scriptId))
  {
    Stop(scriptId, waitShutdown);
    return ETIMEDOUT;
  }

  return 0;
}

bool CScriptInvocationManager::Stop(int scriptId, bool wait /* = false */)
{
  if (scriptId < 0)
    return false;

  std::unique_lock lock(m_critSection);
  auto invokerThread = getInvokerThread(scriptId).thread;
  if (invokerThread == nullptr)
    return false;

  return invokerThread->Stop(wait);
}

void CScriptInvocationManager::StopRunningScripts(bool wait /* = false */)
{
  std::ranges::for_each(m_scripts,
                        [wait, this](auto& script)
                        {
                          if (!script.second.done)
                            Stop(script.second.script, wait);
                        });
}

void CScriptInvocationManager::NotifyAllScriptsAborting()
{
  std::unique_lock lock(m_critSection);
#ifdef TARGET_WEBOS
  // LOCAL PATCH (megarushing): one 5 s budget shared by all service-stop waits,
  // rather than a fresh 5 s for each holdout.
  if (!m_shutdownDeadline)
    m_shutdownDeadline = std::chrono::steady_clock::now() + SCRIPT_STOP_TIMEOUT;
#endif
  for (const auto& handler : m_invocationHandlers)
    handler.second->NotifyScriptAborting(nullptr);
}

bool CScriptInvocationManager::Stop(const std::string &scriptPath, bool wait /* = false */)
{
  if (scriptPath.empty())
    return false;

  std::unique_lock lock(m_critSection);
  const auto script = m_scriptPaths.find(scriptPath);
  if (script == m_scriptPaths.end())
    return false;

  return Stop(script->second, wait);
}

bool CScriptInvocationManager::IsRunning(int scriptId) const
{
  std::unique_lock lock(m_critSection);
  LanguageInvokerThread invokerThread = getInvokerThread(scriptId);
  if (invokerThread.thread == nullptr)
    return false;

  // LOCAL PATCH (megarushing): a pooled thread stays alive between runs. Its run is over once the
  // script is done, even if it never called endOfDirectory (else the caller waits forever).
  if (invokerThread.pooled && !invokerThread.done &&
      invokerThread.thread->GetState() >= InvokerStateScriptDone)
    return false;

  return !invokerThread.done;
}

bool CScriptInvocationManager::IsRunning(const std::string& scriptPath) const
{
  std::unique_lock lock(m_critSection);
  const auto it = m_scriptPaths.find(scriptPath);
  if (it == m_scriptPaths.end())
    return false;

  return IsRunning(it->second);
}

void CScriptInvocationManager::OnExecutionDone(int scriptId)
{
  if (scriptId < 0)
    return;

  std::unique_lock lock(m_critSection);
  const auto script = m_scripts.find(scriptId);
  if (script != m_scripts.end())
    script->second.done = true;
}

CScriptInvocationManager::LanguageInvokerThread CScriptInvocationManager::getInvokerThread(int scriptId) const
{
  if (scriptId < 0)
    return LanguageInvokerThread();

  const auto script = m_scripts.find(scriptId);
  if (script == m_scripts.end())
    return LanguageInvokerThread();

  return script->second;
}
