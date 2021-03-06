# <a id="configuration"></a> Configuration

The general configuration is located in `/etc/elasticarmor/config.ini`.

## <a id="configuration-logging"></a> Logging

You can choose between syslog and file logging. Below are the available options
for each backend and their default values. The default backend is syslog.

### <a id="configuration-logging-syslog"></a> Syslog

    [logging]
    log="syslog"
    level="error"
    facility="authpriv"
    application="elasticarmor"

### <a id="configuration-logging-file"></a> File

    [logging]
    log="file"
    level="error"
    file="/var/log/elasticarmor/elasticarmor.log"

## <a id="configuration-proxy"></a> Proxy

This section allows to configure the internal HTTP reverse proxy. Below are the default values:

    [proxy]
    address="localhost"
    port="59200"
    secured="false"
    elasticsearch="localhost:9200"

### <a id="configuration-proxy-https"></a> HTTPS

To enable TLSv1 encryption on the socket the proxy is listening on set the *secured* option to "true". The options
*private_key* and *certificate* are used to define the path to the private key and certificate to be used.

    [proxy]
    ...
    secured="true"
    private_key="<path>"
    certificate="<path>"

### <a id="configuration-proxy-anonymous-access"></a> Anonymous Access

By default, anonymous requests will be refused and the client is challenged to authenticate. To permit anonymous
access, it is possible to configure a list of hosts from which anonymous requests are permitted. Note that such
requests are still subject to authorization. (i.e. They are required to be associated with at least a single role)

You can set a list of host[:port] combinations separated by comma on the option *allow_from*:

    [proxy]
    ...
    allow_from="localhost, name.example.com, 10.20.30.40:1234"

### <a id="configuration-proxy-trusted-proxies"></a> Trusted Proxies

Proxies configured as being trusted allow ElasticArmor to utilize a request's HTTP headers for authorization.

You can set a list of host[:port] combinations separated by comma on the option *trusted_proxies*:

    [proxy]
    ...
    trusted_proxies="localhost, name.example.com, 10.20.30.40:1234"

The HTTP headers utilized by ElasticArmor for authorization are as follows:

#### <a id="configuration-proxy-trusted-proxies-authorization"></a> Authorization

In case ElasticArmor does not authenticate clients on its own, the *Authorization* header is only used for
identification purposes. This means that requests containing authentication credentials received from a
trusted proxy are considered as being successfully authenticated.

> **Note:**
>
> In case ElasticArmor authenticates clients on its own, they still need to authenticate with the configured
> mechanisms regardless from whom the request has been received.

#### <a id="configuration-proxy-trusted-proxies-x-forwarded-for"></a> X-Forwarded-For

The XFF header is used to replace a request's origin address and port with the very first entry supplied with
this header.

> **Note:**
>
> If you have anonymous access configured, the extracted entry is subject to authorization instead.

### <a id="configuration-proxy-fallback-nodes"></a> Fallback Nodes

It is possible to define multiple Elasticsearch nodes, each separated by comma:

    [proxy]
    ...
    elasticsearch="elasticsearch1:9200, elasticsearch2:9200, elasticsearch3:9200"

The order in which nodes are listed is significant as the first one is the primary node and all other ones are
secondary nodes. Secondary nodes are only used in case the primary node gets unavailable. The first secondary
node is the first one tried in this case and if this does not succeed or if it gets unavailable after some time
as well, the next secondary node is tried. This continues until all secondary nodes have been tried. Nodes
previously marked as unavailable are retried every 15 minutes.
