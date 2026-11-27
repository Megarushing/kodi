/*
 *  Copyright (C) 2013-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#pragma once

#include "addons/IAddon.h"
#include "interfaces/generic/ILanguageInvoker.h"
#include "threads/CriticalSection.h"

#include <chrono>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <vector>

class CLanguageInvokerThread;

class CScriptInvocationManager
{
public:
  static CScriptInvocationManager& GetInstance();

  void Process();
  void Uninitialize();

  void RegisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler, const std::string &extension);
  void RegisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler, const std::set<std::string> &extensions);
  void UnregisterLanguageInvocationHandler(ILanguageInvocationHandler *invocationHandler);
  bool HasLanguageInvoker(const std::string &script) const;
  std::shared_ptr<ILanguageInvoker> GetLanguageInvoker(const std::string& script);

  /*!
  * \brief Claims an idle reusable plugin invoker for the script and returns its addon_handle,
  * or -1 when none is idle (the caller then gets a new handle and a new invoker).
  * LOCAL PATCH (megarushing): a pool of warm invokers per plugin script, see InvokerPool below.
  */
  int GetReusablePluginHandle(const std::string& script);

  /*!
   * \brief Stops every idle pooled plugin invoker so its Python interpreter is freed.
   * LOCAL PATCH (megarushing): used by the Android memory guard.
   * \return number of invokers released
   */
  size_t ReleaseIdlePooledInvokers(const char* reason);

  /*!
   * \brief Executes the given script asynchronously in a separate thread.
   *
   * \param script Path to the script to be executed
   * \param addon (Optional) Addon to which the script belongs
   * \param arguments (Optional) List of arguments passed to the script
   * \return -1 if an error occurred, otherwise the ID of the script
   */
  int ExecuteAsync(const std::string& script,
                   const ADDON::AddonPtr& addon = ADDON::AddonPtr(),
                   const std::vector<std::string>& arguments = std::vector<std::string>(),
                   bool reuseable = false,
                   int pluginHandle = -1);
  /*!
  * \brief Executes the given script asynchronously in a separate thread.
  *
  * \param script Path to the script to be executed
  * \param languageInvoker Language invoker to be used to execute the script
  * \param addon (Optional) Addon to which the script belongs
  * \param arguments (Optional) List of arguments passed to the script
  * \return -1 if an error occurred, otherwise the ID of the script
  */
  int ExecuteAsync(const std::string& script,
                   const std::shared_ptr<ILanguageInvoker>& languageInvoker,
                   const ADDON::AddonPtr& addon = ADDON::AddonPtr(),
                   const std::vector<std::string>& arguments = std::vector<std::string>(),
                   bool reuseable = false,
                   int pluginHandle = -1);

  /*!
  * \brief Executes the given script synchronously.
  *
  * \details The script is actually executed asynchronously but the calling
  * thread is blocked until either the script has finished or the given timeout
  * has expired. If the given timeout has expired the script's execution is
  * stopped and depending on the specified wait behaviour we wait for the
  * script's execution to finish or not.
  *
  * \param script Path to the script to be executed
  * \param addon (Optional) Addon to which the script belongs
  * \param arguments (Optional) List of arguments passed to the script
  * \param timeout (Optional) Timeout (in milliseconds) for the script's execution
  * \param waitShutdown (Optional) Whether to wait when having to forcefully stop the script's execution or not.
  * \return -1 if an error occurred, 0 if the script terminated or ETIMEDOUT if the given timeout expired
  */
  int ExecuteSync(const std::string& script,
                  const ADDON::AddonPtr& addon = ADDON::AddonPtr(),
                  const std::vector<std::string>& arguments = std::vector<std::string>(),
                  uint32_t timeoutMs = 0,
                  bool waitShutdown = false);
  /*!
  * \brief Executes the given script synchronously.
  *
  * \details The script is actually executed asynchronously but the calling
  * thread is blocked until either the script has finished or the given timeout
  * has expired. If the given timeout has expired the script's execution is
  * stopped and depending on the specified wait behaviour we wait for the
  * script's execution to finish or not.
  *
  * \param script Path to the script to be executed
  * \param languageInvoker Language invoker to be used to execute the script
  * \param addon (Optional) Addon to which the script belongs
  * \param arguments (Optional) List of arguments passed to the script
  * \param timeout (Optional) Timeout (in milliseconds) for the script's execution
  * \param waitShutdown (Optional) Whether to wait when having to forcefully stop the script's execution or not.
  * \return -1 if an error occurred, 0 if the script terminated or ETIMEDOUT if the given timeout expired
  */
  int ExecuteSync(const std::string& script,
                  const std::shared_ptr<ILanguageInvoker>& languageInvoker,
                  const ADDON::AddonPtr& addon = ADDON::AddonPtr(),
                  const std::vector<std::string>& arguments = std::vector<std::string>(),
                  uint32_t timeoutMs = 0,
                  bool waitShutdown = false);
  bool Stop(int scriptId, bool wait = false);
  bool Stop(const std::string &scriptPath, bool wait = false);

  /*!
   *\brief Stop all running scripts
   *\param wait if kodi should wait for each script to finish (default false)
  */
  void StopRunningScripts(bool wait = false);

  /*!
   *\brief Tell all running scripts that they are about to be stopped
   *\note Stopping a script waits until it has finished. Notifying all
   *      scripts beforehand lets them shut down in parallel.
   */
  void NotifyAllScriptsAborting();
#ifdef TARGET_WEBOS
  // LOCAL PATCH (megarushing): shared Quit deadline set by NotifyAllScriptsAborting().
  std::chrono::milliseconds GetShutdownScriptTimeout(std::chrono::milliseconds fallback) const;
#endif

  bool IsRunning(int scriptId) const;
  bool IsRunning(const std::string& scriptPath) const;

protected:
  friend class CLanguageInvokerThread;

  void OnExecutionDone(int scriptId);

private:
  CScriptInvocationManager() = default;
  CScriptInvocationManager(const CScriptInvocationManager&) = delete;
  CScriptInvocationManager const& operator=(CScriptInvocationManager const&) = delete;
  virtual ~CScriptInvocationManager();

  struct LanguageInvokerThread
  {
    std::shared_ptr<CLanguageInvokerThread> thread;
    std::string script;
    bool done;
    bool pooled = false;
  };
  // LOCAL PATCH (megarushing): warm plugin invokers (reuselanguageinvoker=true), several per
  // script, each with its own fixed plugin handle. An entry is claimed under m_critSection by
  // GetReusablePluginHandle() and executed by ExecuteAsync() with that handle.
  struct PooledInvoker
  {
    std::shared_ptr<CLanguageInvokerThread> thread;
    std::string script;
    int pluginHandle = -1;
    bool claimed = false;
    std::chrono::steady_clock::time_point lastUsed;
  };
  void PrunePoolLocked();
  bool IsPooledThreadDoneLocked(const PooledInvoker& entry) const;
  std::vector<PooledInvoker> m_invokerPool;
  using LanguageInvokerThreadMap = std::map<int, LanguageInvokerThread>;
  using LanguageInvocationHandlerMap = std::map<std::string, ILanguageInvocationHandler*>;

  LanguageInvokerThread getInvokerThread(int scriptId) const;
  int ExecutePooled(const std::string& script,
                    const ADDON::AddonPtr& addon,
                    const std::vector<std::string>& arguments,
                    int pluginHandle);

  LanguageInvocationHandlerMap m_invocationHandlers;
  LanguageInvokerThreadMap m_scripts;
  std::shared_ptr<CLanguageInvokerThread> m_lastInvokerThread;
  int m_lastPluginHandle = -1;

  std::map<std::string, int> m_scriptPaths;
  int m_nextId = 0;
  mutable CCriticalSection m_critSection;
#ifdef TARGET_WEBOS
  // Shared cooperative-stop deadline. Never renewed by an individual script.
  std::optional<std::chrono::steady_clock::time_point> m_shutdownDeadline;
#endif
};
