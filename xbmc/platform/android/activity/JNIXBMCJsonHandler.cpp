/*
 *  Copyright (C) 2018 Christian Browet
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "JNIXBMCJsonHandler.h"

#include "ServiceBroker.h" // LOCAL PATCH (megarushing)
#include "utils/log.h" // LOCAL PATCH (megarushing)

#include "CompileInfo.h"
#include "interfaces/json-rpc/JSONRPC.h"
#include "interfaces/json-rpc/JSONServiceDescription.h"
#include "interfaces/json-rpc/JSONUtils.h"

#include <androidjni/Context.h>
#include <androidjni/jutils-details.hpp>

#include <atomic> // LOCAL PATCH (megarushing)
#include <chrono>
#include <thread>

using namespace jni;

static std::string s_className = std::string(CCompileInfo::GetClass()) + "/XBMCJsonRPC";

void CJNIXBMCJsonHandler::RegisterNatives(JNIEnv *env)
{
  jclass cClass = env->FindClass(s_className.c_str());
  if(cClass)
  {
    JNINativeMethod methods[] =
    {
      {"_requestJSON", "(Ljava/lang/String;)Ljava/lang/String;", (void*)&CJNIXBMCJsonHandler::_requestJSON},
    };

    env->RegisterNatives(cClass, methods, sizeof(methods)/sizeof(methods[0]));
  }
}

// LOCAL PATCH (megarushing): a gate between Java's JSON requests and Kodi's life cycle. Android calls
// _requestJSON from Java threads (XBMCJsonRPC, e.g. the TV-channel sync job) at any time: before Kodi has
// started (SIGSEGV in CLog::CanLogComponent, bedroom Shield 2026-10-15 x2), while it starts (null
// CSettingsComponent in GetAdvancedSettings, bedroom Shield 2026-10-20) and while it stops. CApplication
// opens the gate once it is initialized and closes it at the top of Stop(), then waits for the requests in
// flight before the services are torn down. seq_cst on both atomics: a request either sees the gate closed
// or Close() sees it in flight.
namespace
{
std::atomic<bool> g_jsonOpen{false};
std::atomic<int> g_jsonInFlight{0};

struct InFlight
{
  InFlight() { g_jsonInFlight++; }
  ~InFlight() { g_jsonInFlight--; }
};
} // namespace

void CJNIXBMCJsonHandler::Open()
{
  g_jsonOpen = true;
}

void CJNIXBMCJsonHandler::Close(std::chrono::milliseconds wait)
{
  g_jsonOpen = false;
  // Bounded: a request can wait on the application thread (messenger), which is the thread calling us.
  const auto end = std::chrono::steady_clock::now() + wait;
  while (g_jsonInFlight > 0 && std::chrono::steady_clock::now() < end)
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  if (g_jsonInFlight > 0)
    CLog::Log(LOGWARNING, "JNIXBMCJsonHandler: {} Java JSON request(s) still running at stop",
              g_jsonInFlight.load());
}

jstring CJNIXBMCJsonHandler::_requestJSON(JNIEnv *env, jobject thiz, jstring request)
{
  InFlight inFlight; // LOCAL PATCH (megarushing): counted before the gate is read, see Close()
  if (!g_jsonOpen || !CServiceBroker::IsLoggingUp() || !CServiceBroker::GetSettingsComponent())
    return env->NewStringUTF(
        R"({"jsonrpc":"2.0","id":null,"error":{"code":-32099,"message":"Kodi is not running"}})");

  CJNIClient client;
  CJNITransportLayer transportLayer;

  std::string strRequest = jcast<std::string>(jhstring::fromJNI(request));
  std::string responseData = JSONRPC::CJSONRPC::MethodCall(strRequest, &transportLayer, &client);

  jstring jres = env->NewStringUTF(responseData.c_str());
  return jres;
}

/**********************************/

bool CJNIXBMCJsonHandler::CJNITransportLayer::PrepareDownload(const char *path, CVariant &details, std::string &protocol)
{
  return false;
}

bool CJNIXBMCJsonHandler::CJNITransportLayer::Download(const char *path, CVariant &result)
{
  return false;
}

int CJNIXBMCJsonHandler::CJNITransportLayer::GetCapabilities()
{
  return JSONRPC::Response;
}

int CJNIXBMCJsonHandler::CJNIClient::GetPermissionFlags()
{
  return JSONRPC::OPERATION_PERMISSION_ALL;
}

int CJNIXBMCJsonHandler::CJNIClient::GetAnnouncementFlags()
{
  // Does not support broadcast
  return 0;
}

bool CJNIXBMCJsonHandler::CJNIClient::SetAnnouncementFlags(int flags)
{
  return false;
}
