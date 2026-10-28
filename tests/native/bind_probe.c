/* Minimal 32-bit Winsock probe for tests/test_bindhook.py.
 *
 * It waits for one line on stdin, binds a UDP socket to 0.0.0.0 and prints the
 * address the hook left in it.  If argv[1] is a UDP port, it also exercises
 * unbound sendto, WSASendTo, connect and WSAConnect sockets so every pre-bind
 * stub can be checked.  Build with the x86 MSVC toolchain (see test_bindhook.py).
 */

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <stdio.h>
#include <stdlib.h>

int main(int argc, char **argv)
{
    WSADATA wsa;
    char line[64];
    SOCKET s, s2;
    struct sockaddr_in addr, got;
    WSABUF wb;
    DWORD wsent;
    int len, rc, sent;

    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0)
        return 2;
    printf("ready\n");
    fflush(stdout);
    if (!fgets(line, sizeof line, stdin))
        return 0;

    s = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
    ZeroMemory(&addr, sizeof addr);
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = 0;
    rc = bind(s, (struct sockaddr *)&addr, sizeof addr);
    len = sizeof got;
    ZeroMemory(&got, sizeof got);
    getsockname(s, (struct sockaddr *)&got, &len);
    printf("bind %s %u %d\n", inet_ntoa(got.sin_addr), ntohs(got.sin_port), rc);
    fflush(stdout);
    closesocket(s);

    if (argc > 1) {
        SOCKET srv, cli, conn;
        struct sockaddr_in peer;
        int plen;

        s2 = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        ZeroMemory(&addr, sizeof addr);
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = inet_addr("127.0.0.1");
        addr.sin_port = htons((u_short)atoi(argv[1]));
        sent = sendto(s2, "x", 1, 0, (struct sockaddr *)&addr, sizeof addr);
        len = sizeof got;
        ZeroMemory(&got, sizeof got);
        getsockname(s2, (struct sockaddr *)&got, &len);
        printf("send %s %u %d\n", inet_ntoa(got.sin_addr), ntohs(got.sin_port), sent);
        fflush(stdout);
        closesocket(s2);

        s2 = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        wb.buf = "y";
        wb.len = 1;
        wsent = 0;
        WSASendTo(s2, &wb, 1, &wsent, 0, (struct sockaddr *)&addr, sizeof addr, NULL, NULL);
        len = sizeof got;
        ZeroMemory(&got, sizeof got);
        getsockname(s2, (struct sockaddr *)&got, &len);
        printf("wsasend %s %u\n", inet_ntoa(got.sin_addr), ntohs(got.sin_port));
        fflush(stdout);
        closesocket(s2);

        srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        ZeroMemory(&addr, sizeof addr);
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = INADDR_ANY;
        addr.sin_port = 0;
        bind(srv, (struct sockaddr *)&addr, sizeof addr);
        listen(srv, 1);
        len = sizeof addr;
        getsockname(srv, (struct sockaddr *)&addr, &len);
        cli = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        connect(cli, (struct sockaddr *)&addr, sizeof addr);
        plen = sizeof peer;
        ZeroMemory(&peer, sizeof peer);
        conn = accept(srv, (struct sockaddr *)&peer, &plen);
        len = sizeof got;
        ZeroMemory(&got, sizeof got);
        getsockname(cli, (struct sockaddr *)&got, &len);
        printf("connect %s %u %s\n", inet_ntoa(got.sin_addr), ntohs(got.sin_port),
               inet_ntoa(peer.sin_addr));
        fflush(stdout);
        closesocket(conn);
        closesocket(cli);
        closesocket(srv);

        srv = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        ZeroMemory(&addr, sizeof addr);
        addr.sin_family = AF_INET;
        addr.sin_addr.s_addr = INADDR_ANY;
        addr.sin_port = 0;
        bind(srv, (struct sockaddr *)&addr, sizeof addr);
        listen(srv, 1);
        len = sizeof addr;
        getsockname(srv, (struct sockaddr *)&addr, &len);
        cli = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        WSAConnect(cli, (struct sockaddr *)&addr, sizeof addr, NULL, NULL, NULL, NULL);
        plen = sizeof peer;
        ZeroMemory(&peer, sizeof peer);
        conn = accept(srv, (struct sockaddr *)&peer, &plen);
        len = sizeof got;
        ZeroMemory(&got, sizeof got);
        getsockname(cli, (struct sockaddr *)&got, &len);
        printf("wsaconnect %s %u %s\n", inet_ntoa(got.sin_addr), ntohs(got.sin_port),
               inet_ntoa(peer.sin_addr));
        fflush(stdout);
        closesocket(conn);
        closesocket(cli);
        closesocket(srv);
    }

    fgets(line, sizeof line, stdin);
    WSACleanup();
    return 0;
}
