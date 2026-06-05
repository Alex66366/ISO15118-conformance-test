/*!
    \file thread.inl
    \brief Thread inline implementation
    \author Ivan Shynkarenka
    \date 27.01.2016
    \copyright MIT License
*/

// CRITICAL: Define _WIN32_WINNT BEFORE <thread> for MinGW
#if (defined(__MINGW32__) || defined(__MINGW64__)) && defined(_WIN32)
  #ifndef _WIN32_WINNT
    #define _WIN32_WINNT 0x0601
  #endif
#endif

#include <thread>

namespace CppCommon {

template <class Fn, class... Args>
inline std::thread Thread::Start(Fn&& fn, Args&&... args)
{
    return std::thread([fn = fn, args...]()
    {
        // Setup exception handler for the new thread
        ExceptionsHandler::SetupThread();

        // Call the base thread function
        fn(std::move(args)...);
    });
}

} // namespace CppCommon
